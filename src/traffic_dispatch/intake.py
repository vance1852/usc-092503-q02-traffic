"""报警受理的标准化、相似度评分与可解释匹配。

所有函数都是确定性的纯计算：同一对报警信号永远得到相同的分数、
置信度和中文解释组件，便于值班长复核与后台还原并案原因。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence


# 各维度权重：地点 45 + 时间 25 + 车辆 25 + 电话 12 + 姓名 8 = 满分 115。
LOCATION_EXACT_SCORE = 45
LOCATION_PARTIAL_SCORE = 28
TIME_SCORES = ((5, 25), (15, 18), (30, 12), (60, 6))
VEHICLE_EXACT_SCORE = 25
VEHICLE_SUFFIX_SCORE = 12
CONTACT_MATCH_SCORE = 12
NAME_MATCH_SCORE = 8

# 达到 AUTO_MERGE_THRESHOLD 自动并案；达到 REVIEW_THRESHOLD 交由值班长裁决。
AUTO_MERGE_THRESHOLD = 70
REVIEW_THRESHOLD = 40

_LOCATION_SYNONYMS = (
    ("高速公路", "高速"),
    ("高架路", "高架"),
    ("公里处", "km"),
    ("公里", "km"),
)
_LOCATION_STRIP = re.compile(r"[\s,，、。.;；:：()（）\[\]【】\-—_/\\|]+")
_PLATE_STRIP = re.compile(r"[^0-9A-Za-z一-鿿]+")
_CONTACT_STRIP = re.compile(r"[\s\-()（）]+")
_WHITESPACE = re.compile(r"\s+")


def standardize_location(raw: str) -> str:
    """把自由文本地点归一成可比较的键：全半角统一、去标点空白、同义词归并。"""
    text = unicodedata.normalize("NFKC", raw).lower()
    text = _LOCATION_STRIP.sub("", text)
    for source, target in _LOCATION_SYNONYMS:
        text = text.replace(source, target)
    return text


def normalize_plate(raw: str) -> str:
    """号牌归一：全半角统一、大写、只保留字母数字与汉字（新能源号牌含汉字）。"""
    text = unicodedata.normalize("NFKC", raw).upper()
    return _PLATE_STRIP.sub("", text)


def normalize_contact(raw: str) -> str:
    """联系电话归一：去掉空白、括号和连接符，保留数字与可能的国际字冠。"""
    text = unicodedata.normalize("NFKC", raw).strip()
    return _CONTACT_STRIP.sub("", text)


def normalize_name(raw: str) -> str:
    """姓名归一：全半角统一并去除全部空白。"""
    return _WHITESPACE.sub("", unicodedata.normalize("NFKC", raw))


def mask_contact(normalized: str) -> str:
    """联系人电话掩码，查询与接口只暴露该形态。"""
    if len(normalized) <= 4:
        return "*" * len(normalized)
    if len(normalized) <= 7:
        return normalized[:2] + "*" * (len(normalized) - 4) + normalized[-2:]
    return normalized[:3] + "*" * (len(normalized) - 7) + normalized[-4:]


def mask_name(normalized: str) -> str:
    """报案人姓名掩码，只保留首字。"""
    if not normalized:
        return "*"
    return normalized[0] + "*" * (len(normalized) - 1)


def contact_digest(normalized: str) -> str:
    return hashlib.sha256(f"contact:{normalized}".encode("utf-8")).hexdigest()


def name_digest(normalized: str) -> str:
    return hashlib.sha256(f"name:{normalized}".encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AlarmSignal:
    """参与匹配的报警信号，只含归一化与哈希后的字段，不含明文隐私。"""

    location_standardized: str
    occurred_at: datetime
    vehicle_plates: tuple[str, ...]
    contact_sha256: str
    name_sha256: str | None


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    """单个匹配维度的得分与中文解释。"""

    dimension: str
    score: int
    max_score: int
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "score": self.score,
            "max_score": self.max_score,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class MatchResult:
    """一对报警的匹配结论：总分、置信度和逐维度解释。"""

    score: int
    confidence: str
    components: tuple[ScoreComponent, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "score": self.score,
            "confidence": self.confidence,
            "components": [component.as_dict() for component in self.components],
        }


def _location_score(left: str, right: str) -> ScoreComponent:
    if left == right:
        return ScoreComponent("location", LOCATION_EXACT_SCORE, LOCATION_EXACT_SCORE, f"标准化地点一致：{left}")
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) >= 4 and shorter in longer:
        return ScoreComponent(
            "location",
            LOCATION_PARTIAL_SCORE,
            LOCATION_EXACT_SCORE,
            f"地点部分重合：「{shorter}」是「{longer}」的一部分",
        )
    return ScoreComponent("location", 0, LOCATION_EXACT_SCORE, "标准化地点不一致")


def _time_score(left: datetime, right: datetime) -> ScoreComponent:
    minutes = abs((left - right).total_seconds()) / 60
    max_score = TIME_SCORES[0][1]
    for limit, score in TIME_SCORES:
        if minutes <= limit:
            return ScoreComponent("time", score, max_score, f"事发时间相差 {minutes:.0f} 分钟，在 {limit} 分钟邻近窗口内")
    return ScoreComponent("time", 0, max_score, f"事发时间相差 {minutes:.0f} 分钟，超出 60 分钟邻近窗口")


def _vehicle_score(left: Sequence[str], right: Sequence[str]) -> ScoreComponent:
    common = sorted(set(left) & set(right))
    if common:
        return ScoreComponent(
            "vehicle", VEHICLE_EXACT_SCORE, VEHICLE_EXACT_SCORE, f"涉事号牌一致：{'、'.join(common)}"
        )
    for plate_a in left:
        for plate_b in right:
            if len(plate_a) >= 4 and len(plate_b) >= 4 and plate_a[-4:] == plate_b[-4:]:
                return ScoreComponent(
                    "vehicle",
                    VEHICLE_SUFFIX_SCORE,
                    VEHICLE_EXACT_SCORE,
                    f"号牌尾四位相近：{plate_a[-4:]}",
                )
    return ScoreComponent("vehicle", 0, VEHICLE_EXACT_SCORE, "无一致或相近号牌")


def _contact_score(left: str, right: str) -> ScoreComponent:
    if left == right:
        return ScoreComponent(
            "contact", CONTACT_MATCH_SCORE, CONTACT_MATCH_SCORE, "报案联系电话一致（哈希比对）"
        )
    return ScoreComponent("contact", 0, CONTACT_MATCH_SCORE, "报案联系电话不一致")


def _name_score(left: str | None, right: str | None) -> ScoreComponent:
    if left and right and left == right:
        return ScoreComponent("person", NAME_MATCH_SCORE, NAME_MATCH_SCORE, "报案人姓名一致（哈希比对）")
    return ScoreComponent("person", 0, NAME_MATCH_SCORE, "报案人姓名不一致或缺失")


def score_signals(current: AlarmSignal, existing: AlarmSignal) -> MatchResult:
    """对两条报警信号打分并给出置信度：auto 自动并案、review 值班长裁决、none 不构成候选。"""
    components = (
        _location_score(current.location_standardized, existing.location_standardized),
        _time_score(current.occurred_at, existing.occurred_at),
        _vehicle_score(current.vehicle_plates, existing.vehicle_plates),
        _contact_score(current.contact_sha256, existing.contact_sha256),
        _name_score(current.name_sha256, existing.name_sha256),
    )
    total = sum(component.score for component in components)
    if total >= AUTO_MERGE_THRESHOLD:
        confidence = "auto"
    elif total >= REVIEW_THRESHOLD:
        confidence = "review"
    else:
        confidence = "none"
    return MatchResult(total, confidence, components)
