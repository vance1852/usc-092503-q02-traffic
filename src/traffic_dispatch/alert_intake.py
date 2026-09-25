"""报警受理、候选合并、值班长裁决与拆分溯源的事务用例。"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .alerts import (
    SOURCE_LABELS,
    AlertInput,
    Location,
    alert_fingerprint,
    location_tokens,
    mask_name,
    mask_phone,
    score_alert_pair,
    score_text,
)
from .clock import parse_utc, utc_text
from .errors import InvalidState, NotFound, ValidationFailed, Conflict
from .planning import canonical_json
from .storage import transaction


def _load_alert_input(row: sqlite3.Row) -> AlertInput:
    """从存储行重建 AlertInput，供评分函数复用。"""
    return AlertInput(
        report_id=row["report_id"],
        idempotency_key=row["idempotency_key"],
        source=row["source"],
        source_ref=row["source_ref"],
        location=Location(
            text=row["location_text"],
            key=row["location_key"],
            tokens=location_tokens(row["location_key"]),
            lat=None if row["lat"] is None else Decimal(row["lat"]),
            lng=None if row["lng"] is None else Decimal(row["lng"]),
        ),
        incident_kind=row["incident_kind"],
        description=row["description"],
        occurred_at=row["occurred_at"],
        contacts=tuple(_Simple(**item) for item in json.loads(row["contacts_json"])),
        vehicles=tuple(_Simple(**item) for item in json.loads(row["vehicles_json"])),
        persons=tuple(_Simple(**item) for item in json.loads(row["persons_json"])),
        plates_in_text=frozenset(json.loads(row["plates_json"])),
    )


class _Simple:
    """兼容 contacts/vehicles/persons 字段的轻量只读对象。"""

    __slots__ = ("name", "phone", "relation", "plate", "kind", "color", "role")

    def __init__(self, **kwargs: Any) -> None:
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot, ""))


class AlertIntakeMixin:
    """依赖宿主类提供 connection / clock / _now / _require / _audit。"""

    # ---- 受理 ----
    def accept_alert(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "alert.write")
        alert = AlertInput.from_dict(raw)
        request_digest = hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()
        fingerprint = alert_fingerprint(alert)

        with transaction(self.connection, immediate=True):
            # 幂等与指纹查重都在写事务内，避免并发受理各自建案
            stored = self.connection.execute(
                "SELECT request_sha256,response_json FROM traffic_idempotency "
                "WHERE scope='alert_report' AND idempotency_key=?",
                (alert.idempotency_key,),
            ).fetchone()
            if stored is not None:
                if stored["request_sha256"] != request_digest:
                    raise Conflict("幂等键对应不同报警内容")
                return json.loads(stored["response_json"])
            if self.connection.execute(
                "SELECT 1 FROM alert_reports WHERE report_id=?", (alert.report_id,)
            ).fetchone() is not None:
                raise Conflict("报警编号已经存在")

            duplicate = self.connection.execute(
                "SELECT r.* FROM alert_report_fingerprints f "
                "JOIN alert_reports r ON r.report_id=f.report_id "
                "WHERE f.fingerprint_sha256=? ORDER BY f.rowid_fingerprint ASC LIMIT 1",
                (fingerprint,),
            ).fetchone()
            if duplicate is not None:
                self.connection.execute(
                    "INSERT INTO alert_timeline_events(report_id,case_id,event_type,detail_json,actor_id,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        duplicate["report_id"], duplicate["case_id"], "duplicate_submission",
                        canonical_json({
                            "submitted_report_id": alert.report_id,
                            "source": alert.source,
                            "source_ref": alert.source_ref,
                            "reason": "来源、联系人、地点、事发时间一致，判定为重复报警",
                        }),
                        actor_id, self._now(),
                    ),
                )
                response = self._receipt(duplicate, duplicate_of=duplicate["report_id"])
                self._store_alert_idempotency(alert.idempotency_key, request_digest, response)
                self._audit("alert_report", alert.report_id, "alert.duplicate_detected", actor_id,
                            {"original_report_id": duplicate["report_id"], "case_id": duplicate["case_id"]})
                return response

            now = self._now()
            case_id = alert.report_id
            self.connection.execute(
                "INSERT INTO incident_cases(case_id,incident_kind,state,created_by,created_at) VALUES(?,?,?,?,?)",
                (case_id, alert.incident_kind, "open", actor_id, now),
            )
            self.connection.execute(
                "INSERT INTO alert_reports(report_id,idempotency_key,case_id,source,source_ref,incident_kind,"
                "location_text,location_key,lat,lng,description,occurred_at,received_at,contacts_json,vehicles_json,"
                "persons_json,plates_json,content_sha256,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    alert.report_id, alert.idempotency_key, case_id, alert.source, alert.source_ref,
                    alert.incident_kind, alert.location.text, alert.location.key,
                    None if alert.location.lat is None else str(alert.location.lat),
                    None if alert.location.lng is None else str(alert.location.lng),
                    alert.description, alert.occurred_at, now,
                    canonical_json([contact.stored() for contact in alert.contacts]),
                    canonical_json([vehicle.stored() for vehicle in alert.vehicles]),
                    canonical_json([person.stored() for person in alert.persons]),
                    canonical_json(sorted(alert.plates_in_text)),
                    fingerprint, actor_id, now,
                ),
            )
            self.connection.execute(
                "INSERT INTO alert_report_fingerprints(report_id,fingerprint_sha256,created_at) VALUES(?,?,?)",
                (alert.report_id, fingerprint, now),
            )
            self._timeline(alert.report_id, case_id, "received", {
                "source": alert.source,
                "source_label": SOURCE_LABELS.get(alert.source, alert.source),
                "source_ref": alert.source_ref,
                "occurred_at": alert.occurred_at,
                "received_at": now,
            }, actor_id)

            best_auto = self._generate_candidates(alert)
            merged_into = case_id
            if best_auto is not None:
                target_case = best_auto["right_case_id"] if best_auto["left_case_id"] == case_id else best_auto["left_case_id"]
                # 若目标案件已并入他案，沿链接找到当前案件
                target_case = self._current_case(target_case)
                if target_case != case_id:
                    self._merge_cases(target_case, case_id, "高置信度自动并案", actor_id,
                                      related_candidate_id=best_auto["candidate_id"])
                    merged_into = target_case
                    self.connection.execute(
                        "UPDATE alert_merge_candidates SET state='accepted',decided_by=?,decided_at=?,"
                        "decision_note=? WHERE candidate_id=?",
                        (actor_id, now, "高置信度自动并案", best_auto["candidate_id"]),
                    )
            report_row = self.connection.execute(
                "SELECT * FROM alert_reports WHERE report_id=?", (alert.report_id,)
            ).fetchone()
            response = self._receipt(report_row, merged_into=(merged_into if merged_into != case_id else None))
            self._store_alert_idempotency(alert.idempotency_key, request_digest, response)
            self._audit("alert_report", alert.report_id, "alert.received", actor_id,
                        {"case_id": merged_into, "auto_merged": merged_into != case_id})
            return response

    def _store_alert_idempotency(self, key: str, request_digest: str, response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO traffic_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES('alert_report',?,?,?,?)",
            (key, request_digest, canonical_json(response), self._now()),
        )

    def _generate_candidates(self, alert: AlertInput) -> dict[str, Any] | None:
        """与 ±120 分钟内、同类型且案件仍开放的既有报警评分，落库候选。返回最佳 auto 候选。"""
        occurred = parse_utc(alert.occurred_at)
        window_start = utc_text(occurred - _dt.timedelta(minutes=120))
        window_end = utc_text(occurred + _dt.timedelta(minutes=120))
        rows = self.connection.execute(
            "SELECT r.* FROM alert_reports r JOIN incident_cases c ON c.case_id=r.case_id "
            "WHERE r.report_id<>? AND r.incident_kind=? AND c.state IN ('open','merged') "
            "AND r.occurred_at BETWEEN ? AND ? ORDER BY r.occurred_at",
            (alert.report_id, alert.incident_kind, window_start, window_end),
        ).fetchall()
        best_auto: dict[str, Any] | None = None
        now = self._now()
        for row in rows:
            other = _load_alert_input(row)
            explanation = score_alert_pair(alert, other)
            state = "rejected" if explanation.band == "reject" else "proposed"
            cursor = self.connection.execute(
                "INSERT INTO alert_merge_candidates(left_report_id,right_report_id,left_case_id,right_case_id,"
                "score,band,explanation_json,state,created_at,decided_at,decision_note) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    alert.report_id, row["report_id"], alert.report_id, row["case_id"],
                    score_text(explanation.score), explanation.band,
                    canonical_json(explanation.as_dict()), state, now,
                    now if state == "rejected" else None,
                    "低于合并阈值，系统未建议合并" if state == "rejected" else "",
                ),
            )
            candidate = {
                "candidate_id": int(cursor.lastrowid),
                "left_case_id": alert.report_id,
                "right_case_id": row["case_id"],
                "score": explanation.score,
            }
            if explanation.band == "auto" and (best_auto is None or explanation.score > best_auto["score"]):
                best_auto = candidate
        return best_auto

    # ---- 候选与裁决 ----
    def list_merge_candidates(self, actor_id: str, state: str = "proposed") -> dict[str, Any]:
        self._require(actor_id, "candidate.read")
        if state not in {"proposed", "accepted", "rejected", "superseded", "all"}:
            raise ValidationFailed("state 过滤值不合法")
        sql = (
            "SELECT * FROM alert_merge_candidates "
            + ("WHERE state=? " if state != "all" else "")
            + "ORDER BY CASE band WHEN 'auto' THEN 0 WHEN 'review' THEN 1 ELSE 2 END, score DESC, candidate_id"
        )
        params: tuple[Any, ...] = (state,) if state != "all" else ()
        rows = self.connection.execute(sql, params).fetchall()
        return {"candidates": [self._candidate_dict(row) for row in rows], "count": len(rows)}

    def decide_merge_candidate(
        self, actor_id: str, candidate_id: int, decision: str, note: str = ""
    ) -> dict[str, Any]:
        self._require(actor_id, "candidate.decide")
        if decision not in {"accepted", "rejected"}:
            raise ValidationFailed("decision 必须是 accepted 或 rejected")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM alert_merge_candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise NotFound("合并候选不存在")
            if row["state"] != "proposed":
                raise InvalidState("该候选已经裁决或已被后续并案取代")
            now = self._now()
            if decision == "rejected":
                self.connection.execute(
                    "UPDATE alert_merge_candidates SET state='rejected',decided_by=?,decided_at=?,decision_note=? "
                    "WHERE candidate_id=?",
                    (actor_id, now, note, candidate_id),
                )
                self._timeline(row["left_report_id"], self._current_case(row["left_case_id"]),
                               "merge_rejected", {"candidate_id": candidate_id, "note": note,
                                                  "paired_report_id": row["right_report_id"]}, actor_id)
                self._audit("merge_candidate", str(candidate_id), "merge.rejected", actor_id,
                            {"left_report_id": row["left_report_id"], "right_report_id": row["right_report_id"], "note": note})
                return self._candidate_dict(
                    self.connection.execute("SELECT * FROM alert_merge_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
                )

            left_case = self._current_case(row["left_case_id"])
            right_case = self._current_case(row["right_case_id"])
            if left_case == right_case:
                self.connection.execute(
                    "UPDATE alert_merge_candidates SET state='superseded',decided_by=?,decided_at=?,"
                    "decision_note=? WHERE candidate_id=?",
                    (actor_id, now, "两案已属同一案件", candidate_id),
                )
                raise InvalidState("两条报警已经属于同一案件")
            # 保留最早报警所在案件为存续案件
            kept, absorbed = self._order_cases(left_case, right_case)
            self._merge_cases(kept, absorbed, note or "值班长裁定合并", actor_id, related_candidate_id=candidate_id)
            self.connection.execute(
                "UPDATE alert_merge_candidates SET state='accepted',decided_by=?,decided_at=?,decision_note=? "
                "WHERE candidate_id=?",
                (actor_id, now, note, candidate_id),
            )
            self._audit("merge_candidate", str(candidate_id), "merge.accepted", actor_id,
                        {"kept_case_id": kept, "absorbed_case_id": absorbed, "note": note})
            return self._candidate_dict(
                self.connection.execute("SELECT * FROM alert_merge_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            )

    def _order_cases(self, left: str, right: str) -> tuple[str, str]:
        earliest = self.connection.execute(
            "SELECT case_id FROM incident_cases WHERE case_id IN (?,?) ORDER BY created_at, rowid",
            (left, right),
        ).fetchall()
        return earliest[0]["case_id"], earliest[1]["case_id"]

    def _merge_cases(
        self,
        kept_case_id: str,
        absorbed_case_id: str,
        reason: str,
        actor_id: str,
        *,
        related_candidate_id: int | None = None,
    ) -> None:
        """把 absorbed 案件的全部报警与已派资源并入 kept；资源绝不取消。"""
        absorbed = self.connection.execute(
            "SELECT * FROM incident_cases WHERE case_id=?", (absorbed_case_id,)
        ).fetchone()
        if absorbed is None:
            raise NotFound("待并案件不存在")
        now = self._now()
        reports = self.connection.execute(
            "SELECT report_id FROM alert_reports WHERE case_id=? ORDER BY occurred_at, report_id",
            (absorbed_case_id,),
        ).fetchall()
        self.connection.execute(
            "UPDATE alert_reports SET case_id=? WHERE case_id=?", (kept_case_id, absorbed_case_id)
        )
        # 已派出资源只改归属，状态与 cancelled_by_merge 一律不动
        self.connection.execute(
            "UPDATE alert_resource_dispatches SET case_id=? WHERE case_id=? AND cancelled_by_merge=0",
            (kept_case_id, absorbed_case_id),
        )
        # 历史时间线事件保留其发生时的案件归属，不改写
        self.connection.execute(
            "UPDATE incident_cases SET state='merged',closed_at=? WHERE case_id=?", (now, absorbed_case_id)
        )
        cursor = self.connection.execute(
            "INSERT INTO incident_case_links(link_type,from_case_id,to_case_id,related_candidate_id,reason,actor_id,created_at) "
            "VALUES('merge',?,?,?,?,?,?)",
            (absorbed_case_id, kept_case_id, related_candidate_id, reason, actor_id, now),
        )
        link_id = int(cursor.lastrowid)
        for item in reports:
            self._timeline(item["report_id"], kept_case_id, "merged", {
                "from_case_id": absorbed_case_id,
                "to_case_id": kept_case_id,
                "link_id": link_id,
                "candidate_id": related_candidate_id,
                "reason": reason,
            }, actor_id)
        # 两案间所有未决候选：若配对的两条报警已在同一案件则随并案失效
        self.connection.execute(
            "UPDATE alert_merge_candidates SET state='superseded',decided_by=?,decided_at=?,"
            "decision_note=? WHERE state='proposed' AND candidate_id IN ("
            "SELECT m.candidate_id FROM alert_merge_candidates m "
            "JOIN alert_reports a ON a.report_id=m.left_report_id "
            "JOIN alert_reports b ON b.report_id=m.right_report_id "
            "WHERE a.case_id=b.case_id AND a.case_id=?)",
            (actor_id, now, f"案件已按链接 {link_id} 合并", kept_case_id),
        )

    # ---- 拆分 ----
    def split_report(self, actor_id: str, report_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "case.split")
        reason = reason.strip() if isinstance(reason, str) else ""
        if not reason:
            raise ValidationFailed("拆分原因不能为空")
        with transaction(self.connection, immediate=True):
            report = self.connection.execute(
                "SELECT * FROM alert_reports WHERE report_id=?", (report_id,)
            ).fetchone()
            if report is None:
                raise NotFound("原始报警不存在")
            old_case_id = self._current_case(report["case_id"])
            peers = self.connection.execute(
                "SELECT COUNT(*) AS n FROM alert_reports WHERE case_id=?", (old_case_id,)
            ).fetchone()["n"]
            if peers < 2:
                raise InvalidState("案件内只有这一条报警，无需拆分")
            merge_link = self.connection.execute(
                "SELECT link_id FROM incident_case_links WHERE link_type='merge' AND to_case_id=? "
                "ORDER BY link_id DESC LIMIT 1",
                (old_case_id,),
            ).fetchone()
            now = self._now()
            new_case_id = f"{report_id}-split"
            suffix = 1
            while self.connection.execute("SELECT 1 FROM incident_cases WHERE case_id=?", (new_case_id,)).fetchone():
                suffix += 1
                new_case_id = f"{report_id}-split{suffix}"
            self.connection.execute(
                "INSERT INTO incident_cases(case_id,incident_kind,state,created_by,created_at) VALUES(?,?,?,?,?)",
                (new_case_id, report["incident_kind"], "open", actor_id, now),
            )
            self.connection.execute(
                "UPDATE alert_reports SET case_id=? WHERE report_id=?", (new_case_id, report_id)
            )
            # 该报警名下资源随报警回到新案件，处置状态原样保留
            self.connection.execute(
                "UPDATE alert_resource_dispatches SET case_id=? WHERE report_id=? AND cancelled_by_merge=0",
                (new_case_id, report_id),
            )
            cursor = self.connection.execute(
                "INSERT INTO incident_case_links(link_type,from_case_id,to_case_id,reverts_link_id,reason,actor_id,created_at) "
                "VALUES('split',?,?,?,?,?,?)",
                (old_case_id, new_case_id,
                 None if merge_link is None else merge_link["link_id"], reason, actor_id, now),
            )
            link_id = int(cursor.lastrowid)
            self._timeline(report_id, new_case_id, "split", {
                "from_case_id": old_case_id,
                "to_case_id": new_case_id,
                "link_id": link_id,
                "reverts_link_id": None if merge_link is None else merge_link["link_id"],
                "reason": reason,
            }, actor_id)
            self._audit("incident_case", new_case_id, "case.split", actor_id,
                        {"report_id": report_id, "from_case_id": old_case_id, "reason": reason, "link_id": link_id})
            return {"case_id": new_case_id, "report_id": report_id, "from_case_id": old_case_id,
                    "link_id": link_id, "state": "open"}

    # ---- 资源登记（不被并案取消）----
    def register_resource_dispatch(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "alert_resource.write")
        resource_id = str(raw.get("resource_dispatch_id", "")).strip()
        report_id = str(raw.get("report_id", "")).strip()
        kind = str(raw.get("resource_kind", "")).strip()
        ref = str(raw.get("resource_ref", "")).strip()
        if not resource_id or not report_id or not kind or not ref:
            raise ValidationFailed("resource_dispatch_id、report_id、resource_kind、resource_ref 均为必填")
        report = self.connection.execute("SELECT * FROM alert_reports WHERE report_id=?", (report_id,)).fetchone()
        if report is None:
            raise NotFound("原始报警不存在")
        case_id = self._current_case(report["case_id"])
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO alert_resource_dispatches(resource_dispatch_id,report_id,case_id,resource_kind,"
                    "resource_ref,state,detail_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (resource_id, report_id, case_id, kind, ref, "dispatched",
                     canonical_json(raw.get("detail", {})), actor_id, now),
                )
                self._timeline(report_id, case_id, "resource_dispatched", {
                    "resource_dispatch_id": resource_id,
                    "resource_kind": kind,
                    "resource_ref": ref,
                }, actor_id)
                self._audit("alert_resource", resource_id, "resource.dispatched", actor_id,
                            {"report_id": report_id, "case_id": case_id, "kind": kind})
        except sqlite3.IntegrityError as exc:
            raise Conflict("资源派警记录编号已存在") from exc
        return {"resource_dispatch_id": resource_id, "report_id": report_id, "case_id": case_id,
                "state": "dispatched", "cancelled_by_merge": 0}

    def cancel_resource_dispatch(self, actor_id: str, resource_dispatch_id: str, reason: str) -> dict[str, Any]:
        """显式取消一条派警/派拖车。并案不会触发本方法——取消必须有人显式负责并留痕。"""
        self._require(actor_id, "alert_resource.write")
        reason = reason.strip() if isinstance(reason, str) else ""
        if not reason:
            raise ValidationFailed("取消原因不能为空")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM alert_resource_dispatches WHERE resource_dispatch_id=?",
                (resource_dispatch_id,),
            ).fetchone()
            if row is None:
                raise NotFound("派警资源记录不存在")
            if row["state"] == "cancelled":
                raise InvalidState("该资源已取消")
            self.connection.execute(
                "UPDATE alert_resource_dispatches SET state='cancelled' WHERE resource_dispatch_id=?",
                (resource_dispatch_id,),
            )
            self._timeline(row["report_id"], row["case_id"], "resource_cancelled", {
                "resource_dispatch_id": resource_dispatch_id,
                "resource_kind": row["resource_kind"],
                "resource_ref": row["resource_ref"],
                "reason": reason,
            }, actor_id)
            self._audit("alert_resource", resource_dispatch_id, "resource.cancelled", actor_id,
                        {"report_id": row["report_id"], "reason": reason})
        return {"resource_dispatch_id": resource_dispatch_id, "state": "cancelled",
                "cancelled_by_merge": 0}

    # ---- 查询与溯源 ----
    def case_detail(self, actor_id: str, case_id: str, *, unmask: bool = False) -> dict[str, Any]:
        self._require(actor_id, "case.read")
        if unmask:
            self._require(actor_id, "contact.read")
        case = self.connection.execute("SELECT * FROM incident_cases WHERE case_id=?", (case_id,)).fetchone()
        if case is None:
            raise NotFound("案件不存在")
        current = self._current_case(case_id)
        current_case = self.connection.execute(
            "SELECT * FROM incident_cases WHERE case_id=?", (current,)
        ).fetchone()
        reports = []
        rows = self.connection.execute(
            "SELECT * FROM alert_reports WHERE case_id=? ORDER BY occurred_at, received_at, report_id",
            (current,),
        ).fetchall()
        for row in rows:
            reports.append(self._report_dict(row, unmask=unmask))
        resources = [
            dict(row) for row in self.connection.execute(
                "SELECT resource_dispatch_id,report_id,resource_kind,resource_ref,state,cancelled_by_merge,"
                "created_by,created_at FROM alert_resource_dispatches WHERE case_id=? ORDER BY created_at",
                (current,),
            ).fetchall()
        ]
        return {
            "case_id": current,
            "requested_case_id": case_id,
            "incident_kind": current_case["incident_kind"],
            "state": current_case["state"],
            "reports": reports,
            "resources": resources,
        }

    def report_timeline(self, actor_id: str, report_id: str) -> dict[str, Any]:
        self._require(actor_id, "case.read")
        report = self.connection.execute("SELECT report_id FROM alert_reports WHERE report_id=?", (report_id,)).fetchone()
        if report is None:
            raise NotFound("原始报警不存在")
        rows = self.connection.execute(
            "SELECT timeline_id,event_type,detail_json,actor_id,created_at,case_id FROM alert_timeline_events "
            "WHERE report_id=? ORDER BY timeline_id",
            (report_id,),
        ).fetchall()
        events = []
        for row in rows:
            events.append({
                "timeline_id": row["timeline_id"],
                "case_id": row["case_id"],
                "event_type": row["event_type"],
                "detail": json.loads(row["detail_json"]),
                "actor_id": row["actor_id"],
                "created_at": row["created_at"],
            })
        return {"report_id": report_id, "events": events}

    def case_lineage(self, actor_id: str, case_id: str) -> dict[str, Any]:
        """还原案件为何合并或分离：合并/拆分链接 + 相关候选的解释与裁决。"""
        self._require(actor_id, "case.read")
        current = self._current_case(case_id)
        if current == case_id:
            link_rows = self.connection.execute(
                "SELECT * FROM incident_case_links WHERE from_case_id=? OR to_case_id=? ORDER BY link_id",
                (case_id, case_id),
            ).fetchall()
        else:
            # 查询的是已被吸收的旧案件：同时覆盖该旧案的链接与当前存续案件的链接
            link_rows = self.connection.execute(
                "SELECT * FROM incident_case_links WHERE from_case_id=? OR to_case_id=? "
                "OR from_case_id=? OR to_case_id=? ORDER BY link_id",
                (case_id, case_id, current, current),
            ).fetchall()
        links = []
        candidate_ids: list[int] = []
        for row in link_rows:
            item = dict(row)
            if row["link_type"] == "merge" and row["related_candidate_id"]:
                candidate_ids.append(row["related_candidate_id"])
            links.append(item)
        candidates = []
        for cid in dict.fromkeys(candidate_ids):
            crow = self.connection.execute(
                "SELECT * FROM alert_merge_candidates WHERE candidate_id=?", (cid,)
            ).fetchone()
            if crow is not None:
                candidates.append(self._candidate_dict(crow))
        # 附上涉及该案全部报警的候选裁决，支撑"为何分离"
        report_rows = self.connection.execute(
            "SELECT report_id FROM alert_reports WHERE case_id=?", (current,)
        ).fetchall()
        report_ids = [row["report_id"] for row in report_rows]
        decisions = []
        if report_ids:
            placeholders = ",".join("?" for _ in report_ids)
            extra = self.connection.execute(
                f"SELECT * FROM alert_merge_candidates WHERE state IN ('accepted','rejected','superseded') "
                f"AND (left_report_id IN ({placeholders}) OR right_report_id IN ({placeholders})) "
                "ORDER BY candidate_id",
                (*report_ids, *report_ids),
            ).fetchall()
            seen = {c["candidate_id"] for c in candidates}
            decisions = [self._candidate_dict(row) for row in extra if row["candidate_id"] not in seen]
        return {"case_id": current, "links": links, "link_decisions": candidates, "other_decisions": decisions}

    # ---- 组装辅助 ----
    def _current_case(self, case_id: str) -> str:
        """沿最新 merge 链接找到案件当前归属。"""
        current = case_id
        for _ in range(100):
            row = self.connection.execute(
                "SELECT to_case_id FROM incident_case_links WHERE link_type='merge' AND from_case_id=? "
                "ORDER BY link_id DESC LIMIT 1",
                (current,),
            ).fetchone()
            if row is None:
                return current
            current = row["to_case_id"]
        raise InvalidState("案件合并链路过深，可能存在环路")

    def _timeline(self, report_id: str, case_id: str, event_type: str, detail: Mapping[str, Any], actor_id: str | None) -> None:
        self.connection.execute(
            "INSERT INTO alert_timeline_events(report_id,case_id,event_type,detail_json,actor_id,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (report_id, case_id, event_type, canonical_json(detail), actor_id, self._now()),
        )

    def _receipt(self, row: sqlite3.Row, *, duplicate_of: str | None = None, merged_into: str | None = None) -> dict[str, Any]:
        case_id = merged_into or row["case_id"]
        return {
            "report_id": row["report_id"],
            "case_id": case_id,
            "received_at": row["received_at"],
            "occurred_at": row["occurred_at"],
            "location": {"text": row["location_text"], "location_key": row["location_key"]},
            "duplicate": duplicate_of is not None or merged_into is not None,
            "duplicate_of_report_id": duplicate_of,
            "auto_merged_into_case_id": merged_into,
        }

    def _report_dict(self, row: sqlite3.Row, *, unmask: bool) -> dict[str, Any]:
        contacts_raw = json.loads(row["contacts_json"])
        persons_raw = json.loads(row["persons_json"])
        if unmask:
            contacts = [{"name": c["name"], "phone": c["phone"], "relation": c["relation"]} for c in contacts_raw]
            persons = list(persons_raw)
        else:
            contacts = [{"name": mask_name(c["name"]), "phone": mask_phone(c["phone"]), "relation": c["relation"]}
                        for c in contacts_raw]
            persons = [{"name": mask_name(p["name"]), "phone": mask_phone(p["phone"]), "role": p["role"]}
                       for p in persons_raw]
        return {
            "report_id": row["report_id"],
            "case_id": self._current_case(row["case_id"]),
            "source": row["source"],
            "source_label": SOURCE_LABELS.get(row["source"], row["source"]),
            "source_ref": row["source_ref"],
            "incident_kind": row["incident_kind"],
            "location": {"text": row["location_text"], "location_key": row["location_key"],
                         "lat": row["lat"], "lng": row["lng"]},
            "description": row["description"],
            "occurred_at": row["occurred_at"],
            "received_at": row["received_at"],
            "contacts": contacts,
            "vehicles": json.loads(row["vehicles_json"]),
            "persons": persons,
            "plates": json.loads(row["plates_json"]),
            "duplicate_of_report_id": row["duplicate_of_report_id"],
        }

    def _candidate_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        explanation = json.loads(row["explanation_json"])
        return {
            "candidate_id": row["candidate_id"],
            "left_report_id": row["left_report_id"],
            "right_report_id": row["right_report_id"],
            "left_case_id": row["left_case_id"],
            "right_case_id": row["right_case_id"],
            "left_current_case_id": self._current_case(row["left_case_id"]),
            "right_current_case_id": self._current_case(row["right_case_id"]),
            "score": row["score"],
            "band": row["band"],
            "state": row["state"],
            "explanation": explanation,
            "decided_by": row["decided_by"],
            "decided_at": row["decided_at"],
            "decision_note": row["decision_note"],
            "created_at": row["created_at"],
        }
