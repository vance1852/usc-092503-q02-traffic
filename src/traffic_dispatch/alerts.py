"""报警受理的标准化线索与可解释匹配评分（纯函数）。

匹配只依赖四类证据：
1. 标准化地点（文字归一化键、道路要素 token、可选经纬度距离）；
2. 时间邻近（报警声称的事发时间）；
3. 车辆线索（车牌强标识，车型/颜色弱标识）；
4. 人员信息（联系电话强标识，姓名弱标识）。

评分输出每个因子的贡献与命中明细，便于接警台、值班长和后台审计解释
"为什么建议合并"或"为什么没有合并"。
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Sequence

from .clock import parse_utc
from .errors import ValidationFailed
from .models import identifier, required_text
from .planning import canonical_json


ALERT_SOURCES = {"caller", "witness", "patrol", "device", "transfer"}
SOURCE_LABELS = {
    "caller": "当事人报警",
    "witness": "路人报警",
    "patrol": "巡逻车上报",
    "device": "设备自动上报",
    "transfer": "其他单位转警",
}

# 评分区间
AUTO_BAND = Decimal("0.82")
REVIEW_BAND = Decimal("0.50")
# 时间硬窗口：超过 120 分钟不再作为同一起事故候选
TIME_GATE = timedelta(minutes=120)

PROVINCE_PLATE = "京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼使领"
PLATE_RE = re.compile(rf"[{PROVINCE_PLATE}][A-Z][A-Z0-9]{{4,6}}")
ROAD_TOKEN_RE = re.compile(
    r"[0-9]+(?:\s*\+\s*[0-9]+)?"
    r"|[一二三四五六七八九十百千万0-9南北东西]+(?:环|高速|快速路|高架|大桥|隧道|路|街|大道|道|巷|桥|匝道|出口|互通|服务区|公里)"
)
ROAD_ANCHOR_RE = re.compile(
    r"([一-龥A-Za-z0-9]{1,12}?(?:高速|快速路|高架路?|大桥|隧道|大道|环路|环|路|街|匝道))"
    r"[^0-9Kk]{0,12}?"
    r"[Kk]?\s*([0-9]{1,4})\s*\+\s*([0-9]{1,3})"
)
ADMIN_PREFIX_RE = re.compile(r"^.*?(省|自治区|市|区|县)")
GENERIC_WORDS = ("附近", "方向", "往", "交叉口", "路口", "门口", "门前", "旁", "边")

VEHICLE_KINDS = {"sedan", "suv", "truck", "bus", "van", "motorcycle", "taxi", "trailer", "other"}
VEHICLE_COLORS = {"white", "black", "silver", "gray", "red", "blue", "yellow", "green", "brown", "orange"}


def score_text(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def normalize_phone(value: object) -> str:
    """保留数字，去掉 +86/86 前缀和分隔符；不足 5 位视为无有效电话。"""
    if not isinstance(value, str):
        return ""
    digits = re.sub(r"\D", "", value)
    if digits.startswith("86") and len(digits) > 8:
        digits = digits[2:]
    return digits if len(digits) >= 5 else ""


def mask_phone(phone: str) -> str:
    digits = normalize_phone(phone)
    if not digits:
        return ""
    if len(digits) >= 7:
        return digits[:3] + "*" * (len(digits) - 7) + digits[-4:]
    return digits[:2] + "*" * (len(digits) - 2)


def mask_name(name: str) -> str:
    name = name.strip()
    if not name:
        return ""
    if len(name) == 1:
        return name
    return name[0] + "*" * (len(name) - 1)


def normalize_plate(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).upper().replace(" ", "")
    match = PLATE_RE.search(normalized)
    return match.group(0) if match else ""


def extract_plates(text: str) -> frozenset[str]:
    if not text:
        return frozenset()
    normalized = unicodedata.normalize("NFKC", text).upper().replace(" ", "")
    return frozenset(PLATE_RE.findall(normalized))


def normalize_location_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).upper()
    normalized = re.sub(r"[\s,，。.、;；:：!！?？()（）\-_#＃]", "", normalized)
    normalized = ADMIN_PREFIX_RE.sub("", normalized, count=1)
    return normalized


def location_tokens(key: str) -> frozenset[str]:
    return frozenset(token.replace(" ", "") for token in ROAD_TOKEN_RE.findall(key))


def decimal_coordinate(value: object, field: str) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - 输入契约统一转 ValidationFailed
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    return result


@dataclass(frozen=True, slots=True)
class Location:
    text: str
    key: str
    tokens: frozenset[str]
    lat: Decimal | None = None
    lng: Decimal | None = None

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "Location":
        text = required_text(raw.get("text"), "location.text", 256)
        key = normalize_location_text(text)
        if not key:
            raise ValidationFailed("location.text 标准化后为空")
        lat = decimal_coordinate(raw.get("lat"), "location.lat")
        lng = decimal_coordinate(raw.get("lng"), "location.lng")
        if (lat is None) != (lng is None):
            raise ValidationFailed("location.lat 与 location.lng 必须同时提供")
        if lat is not None and not (Decimal("-90") <= lat <= Decimal("90")):
            raise ValidationFailed("location.lat 超出范围")
        if lng is not None and not (Decimal("-180") <= lng <= Decimal("180")):
            raise ValidationFailed("location.lng 超出范围")
        return cls(text=text.strip(), key=key, tokens=location_tokens(key), lat=lat, lng=lng)

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "location_key": self.key,
            "lat": None if self.lat is None else str(self.lat),
            "lng": None if self.lng is None else str(self.lng),
        }


@dataclass(frozen=True, slots=True)
class Contact:
    name: str
    phone: str
    relation: str

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "Contact":
        name = required_text(raw.get("name"), "contacts.name", 64)
        phone_digits = normalize_phone(raw.get("phone"))
        if not raw.get("phone") and not name:
            raise ValidationFailed("联系人必须提供姓名或电话")
        relation = required_text(raw.get("relation", "联系人"), "contacts.relation", 32)
        return cls(name=name.strip(), phone=phone_digits, relation=relation.strip())

    def stored(self) -> dict[str, Any]:
        return {"name": self.name, "phone": self.phone, "relation": self.relation}

    def masked(self) -> dict[str, Any]:
        return {"name": mask_name(self.name), "phone": mask_phone(self.phone), "relation": self.relation}


@dataclass(frozen=True, slots=True)
class Vehicle:
    plate: str
    kind: str
    color: str

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "Vehicle":
        plate = normalize_plate(raw.get("plate"))
        kind = required_text(raw.get("kind", "other"), "vehicles.kind", 24).lower()
        if kind not in VEHICLE_KINDS:
            raise ValidationFailed("vehicles.kind 不受支持")
        color = required_text(raw.get("color", "unknown"), "vehicles.color", 24).lower()
        if color not in VEHICLE_COLORS and color != "unknown":
            raise ValidationFailed("vehicles.color 不受支持")
        if not plate and kind == "other" and color == "unknown":
            raise ValidationFailed("车辆线索至少需要车牌、车型或颜色中的一项")
        return cls(plate=plate, kind=kind, color=color)

    def stored(self) -> dict[str, Any]:
        return {"plate": self.plate, "kind": self.kind, "color": self.color}


@dataclass(frozen=True, slots=True)
class Person:
    name: str
    phone: str
    role: str

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "Person":
        name = required_text(raw.get("name"), "persons.name", 64).strip()
        phone = normalize_phone(raw.get("phone"))
        if not name and not phone:
            raise ValidationFailed("人员信息至少需要姓名或电话")
        person_role = required_text(raw.get("role", "party"), "persons.role", 24)
        return cls(name=name, phone=phone, role=person_role.strip())

    def stored(self) -> dict[str, Any]:
        return {"name": self.name, "phone": self.phone, "role": self.role}

    def masked(self) -> dict[str, Any]:
        return {"name": mask_name(self.name), "phone": mask_phone(self.phone), "role": self.role}


def _sequence(value: object, field: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValidationFailed(f"{field} 必须是数组")
    result: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValidationFailed(f"{field} 元素必须是对象")
        result.append(item)
    return result


@dataclass(frozen=True, slots=True)
class AlertInput:
    report_id: str
    idempotency_key: str
    source: str
    source_ref: str
    location: Location
    incident_kind: str
    description: str
    occurred_at: str
    contacts: tuple[Contact, ...]
    vehicles: tuple[Vehicle, ...]
    persons: tuple[Person, ...]
    plates_in_text: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AlertInput":
        report_id = identifier(raw.get("report_id"), "report_id")
        idempotency_key = identifier(raw.get("idempotency_key"), "idempotency_key")
        source = required_text(raw.get("source"), "source", 24).lower()
        if source not in ALERT_SOURCES:
            raise ValidationFailed("source 必须是 caller、witness、patrol、device 或 transfer")
        source_ref = required_text(raw.get("source_ref", ""), "source_ref", 128) if raw.get("source_ref") else ""
        location = Location.from_raw(raw.get("location") if isinstance(raw.get("location"), Mapping) else {})
        incident_kind = required_text(raw.get("incident_kind", "collision"), "incident_kind", 32).lower()
        description = required_text(raw.get("description", ""), "description", 2000) if raw.get("description") else ""
        occurred_text = required_text(raw.get("occurred_at"), "occurred_at", 40)
        try:
            parse_utc(occurred_text, "occurred_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        contacts = tuple(Contact.from_raw(item) for item in _sequence(raw.get("contacts", []), "contacts"))
        if not contacts:
            raise ValidationFailed("contacts 至少包含一名联系人")
        vehicles = tuple(Vehicle.from_raw(item) for item in _sequence(raw.get("vehicles", []), "vehicles"))
        persons = tuple(Person.from_raw(item) for item in _sequence(raw.get("persons", []), "persons"))
        plates = frozenset(vehicle.plate for vehicle in vehicles if vehicle.plate) | extract_plates(description)
        return cls(
            report_id=report_id,
            idempotency_key=idempotency_key,
            source=source,
            source_ref=source_ref,
            location=location,
            incident_kind=incident_kind,
            description=description,
            occurred_at=occurred_text,
            contacts=contacts,
            vehicles=vehicles,
            persons=persons,
            plates_in_text=plates,
        )

    def all_plates(self) -> frozenset[str]:
        return self.plates_in_text

    def phones(self) -> frozenset[str]:
        values = {contact.phone for contact in self.contacts if contact.phone}
        values.update(person.phone for person in self.persons if person.phone)
        return frozenset(values)

    def names(self) -> frozenset[str]:
        values = {contact.name for contact in self.contacts if contact.name}
        values.update(person.name for person in self.persons if person.name)
        return frozenset(values)


def haversine_meters(a: Location, b: Location) -> Decimal | None:
    if a.lat is None or a.lng is None or b.lat is None or b.lng is None:
        return None
    lat1 = math.radians(float(a.lat))
    lat2 = math.radians(float(b.lat))
    dlat = lat2 - lat1
    dlng = math.radians(float(b.lng) - float(a.lng))
    chord = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    distance = 2 * 6371000 * math.asin(math.sqrt(chord))
    return Decimal(str(int(round(distance))))


def road_anchor(key: str) -> tuple[str, int] | None:
    """提取 (归一化道路名, 里程米数) 锚点，例如 "北环高速东行K23+500" -> ("北环高速", 23500)。"""
    match = ROAD_ANCHOR_RE.search(key)
    if match is None:
        return None
    name = normalize_location_text(match.group(1))
    meters = int(match.group(2)) * 1000 + int(match.group(3))
    return name, meters


def location_score(a: Location, b: Location) -> tuple[Decimal, str]:
    """返回 0-1 的地点相似度与判定依据。"""
    if a.key == b.key:
        return Decimal("1"), "标准化地点完全一致"
    distance = haversine_meters(a, b)
    anchor_a = road_anchor(a.key)
    anchor_b = road_anchor(b.key)
    if anchor_a is not None and anchor_b is not None and anchor_a[0] == anchor_b[0]:
        gap = abs(anchor_a[1] - anchor_b[1])
        if gap <= 100:
            return Decimal("1"), f"同一道路里程桩相距 {gap} 米"
        if gap <= 1000:
            value = Decimal("1") - Decimal(gap - 100) / Decimal("900") * Decimal("0.25")
            return value, f"同一道路里程桩相距 {gap} 米"
        if gap <= 3000:
            return Decimal("0.55"), f"同一道路但里程桩相距 {gap} 米"
        return Decimal("0.15"), f"同一道路但里程桩相距 {gap} 米，超出邻近范围"
    if distance is not None:
        if distance <= 150:
            return Decimal("1"), f"坐标相距 {distance} 米"
        if distance <= 1000:
            value = Decimal("1") - (distance - Decimal("150")) / Decimal("850") * Decimal("0.6")
            return max(value, Decimal("0.4")), f"坐标相距 {distance} 米"
        if distance <= 2000:
            return Decimal("0.1"), f"坐标相距 {distance} 米"
        return Decimal("0"), f"坐标相距 {distance} 米，超出邻近范围"
    if a.tokens and b.tokens:
        overlap = len(a.tokens & b.tokens)
        union = len(a.tokens | b.tokens)
        jaccard_value = Decimal(overlap) / Decimal(union)
        scaled = min(Decimal("1"), jaccard_value * Decimal("1.25"))
        if overlap:
            return scaled, f"道路要素重合 {sorted(a.tokens & b.tokens)}"
    return Decimal("0"), "标准化地点不一致"


def time_score(gap: timedelta) -> Decimal:
    seconds = Decimal(int(abs(gap.total_seconds())))
    if seconds <= 600:
        return Decimal("1")
    if seconds <= 1800:
        return Decimal("1") - (seconds - 600) / 1200 * Decimal("0.4")
    if seconds <= 3600:
        return Decimal("0.6") - (seconds - 1800) / 1800 * Decimal("0.4")
    if seconds <= 7200:
        return Decimal("0.2") - (seconds - 3600) / 3600 * Decimal("0.2")
    return Decimal("0")


def jaccard(a: Iterable[str], b: Iterable[str]) -> Decimal:
    left = {item for item in a if item}
    right = {item for item in b if item}
    if not left or not right:
        return Decimal("0")
    return Decimal(len(left & right)) / Decimal(len(left | right))


@dataclass(frozen=True, slots=True)
class MatchExplanation:
    score: Decimal
    band: str
    time_gap_seconds: int
    within_time_gate: bool
    strong_identifier: bool
    reasons: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": score_text(self.score),
            "band": self.band,
            "time_gap_seconds": self.time_gap_seconds,
            "within_time_gate": self.within_time_gate,
            "strong_identifier": self.strong_identifier,
            "reasons": list(self.reasons),
        }


def score_alert_pair(left: AlertInput, right: AlertInput) -> MatchExplanation:
    """对两条报警打匹配分。权重：地点 0.42、时间 0.26、车辆最多 0.26、人员最多 0.10。"""
    reasons: list[dict[str, Any]] = []
    left_time = parse_utc(left.occurred_at)
    right_time = parse_utc(right.occurred_at)
    gap = left_time - right_time
    gap_seconds = int(abs(gap.total_seconds()))
    within_gate = abs(gap) <= TIME_GATE

    loc_value, loc_detail = location_score(left.location, right.location)
    loc_contribution = (loc_value * Decimal("0.42")).quantize(Decimal("0.0001"))
    reasons.append({
        "factor": "location",
        "weight": "0.42",
        "factor_score": score_text(loc_value),
        "contribution": str(loc_contribution),
        "detail": loc_detail,
    })

    time_value = time_score(gap)
    time_contribution = (time_value * Decimal("0.26")).quantize(Decimal("0.0001"))
    reasons.append({
        "factor": "time",
        "weight": "0.26",
        "factor_score": score_text(time_value),
        "contribution": str(time_contribution),
        "detail": f"事发时间相差 {gap_seconds} 秒",
    })

    vehicle_contribution = Decimal("0")
    shared_plates = left.all_plates() & right.all_plates()
    if shared_plates:
        vehicle_contribution += Decimal("0.26")
        reasons.append({
            "factor": "vehicle_plate",
            "weight": "0.26",
            "factor_score": "1",
            "contribution": "0.2600",
            "detail": f"车牌一致：{sorted(shared_plates)}",
        })
    weak_vehicle = jaccard(
        [vehicle.kind for vehicle in left.vehicles if vehicle.kind != "other"]
        + [vehicle.color for vehicle in left.vehicles if vehicle.color != "unknown"],
        [vehicle.kind for vehicle in right.vehicles if vehicle.kind != "other"]
        + [vehicle.color for vehicle in right.vehicles if vehicle.color != "unknown"],
    )
    if not shared_plates and weak_vehicle > 0:
        weak_value = min(Decimal("0.10"), weak_vehicle * Decimal("0.20"))
        vehicle_contribution += weak_value
        reasons.append({
            "factor": "vehicle_traits",
            "weight": "0.10",
            "factor_score": score_text(weak_vehicle),
            "contribution": str(weak_value.quantize(Decimal("0.0001"))),
            "detail": "车型或颜色部分一致",
        })

    person_contribution = Decimal("0")
    shared_phones = left.phones() & right.phones()
    if shared_phones:
        person_contribution += Decimal("0.10")
        reasons.append({
            "factor": "person_phone",
            "weight": "0.10",
            "factor_score": "1",
            "contribution": "0.1000",
            "detail": "联系电话一致",
        })
    shared_names = left.names() & right.names()
    if shared_names:
        person_contribution += Decimal("0.04")
        reasons.append({
            "factor": "person_name",
            "weight": "0.04",
            "factor_score": "1",
            "contribution": "0.0400",
            "detail": f"姓名一致：{sorted(shared_names)}",
        })

    total = (loc_contribution + time_contribution + vehicle_contribution + person_contribution)
    score = min(Decimal("1"), total).quantize(Decimal("0.001"))
    strong = bool(shared_plates) or bool(shared_phones)
    auto_ready = (
        within_gate
        and loc_value >= Decimal("0.99")
        and time_value >= Decimal("0.9")
        and strong
    )
    if not within_gate:
        band = "reject"
    elif auto_ready and score >= AUTO_BAND:
        band = "auto"
    elif score >= REVIEW_BAND:
        band = "review"
    else:
        band = "reject"
    return MatchExplanation(
        score=score,
        band=band,
        time_gap_seconds=gap_seconds,
        within_time_gate=within_gate,
        strong_identifier=strong,
        reasons=tuple(reasons),
    )


def alert_fingerprint(alert: AlertInput) -> str:
    """重复报警的内容指纹：来源 + 联系人电话 + 标准化地点 + 事发分钟 + 车牌集合。"""
    occurred_minute = parse_utc(alert.occurred_at).replace(second=0, microsecond=0).isoformat()
    body = {
        "source": alert.source,
        "phones": sorted(alert.phones()),
        "location_key": alert.location.key,
        "occurred_minute": occurred_minute,
        "plates": sorted(alert.all_plates()),
    }
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
