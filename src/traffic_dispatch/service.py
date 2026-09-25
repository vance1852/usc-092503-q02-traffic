"""风险指数、应急资源库存、道路走廊和调度申请的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .intake import (
    AlarmSignal,
    contact_digest,
    mask_contact,
    mask_name,
    name_digest,
    score_signals,
    standardize_location,
)
from .models import AlarmReport, RiskIndexRecord, ResponseCenter, ResponseResourceLot, DispatchRequest, RoadCorridor, ResponseScenario
from .planning import (
    AllocationRequest,
    RiskPoint,
    allocate_capacity,
    canonical_json,
    decimal_text,
    delivered_after_loss,
    digest,
    effective_capacity,
    latest_streak,
    moving_average,
    quantize_volume,
    scenario_projection,
    weighted_inventory_cost,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"risk_record.write", "catalog.write", "scenario.write", "scenario.run"},
    "dispatcher": {"dispatch_request.write", "allocation.run", "deployment.write", "inventory.write", "alarm.read"},
    "risk": {"outage.write", "scenario.approve", "report.read"},
    "auditor": {"report.read", "audit.read", "alarm.read"},
    "intake": {"alarm.write", "alarm.read"},
    "supervisor": {"alarm.read", "merge.decide", "merge.split"},
}

# 报警候选比对的事发时间邻近窗口。
CANDIDATE_WINDOW = timedelta(hours=2)


class TrafficDispatchService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM traffic_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM traffic_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO traffic_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO traffic_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def record_risk_record(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "risk_record.write")
        risk_record = RiskIndexRecord.from_dict(raw)
        previous = self.connection.execute(
            "SELECT risk_record_id,source_revision FROM risk_index_risk_records WHERE risk_index=? AND duty_date=? "
            "ORDER BY risk_record_id DESC LIMIT 1",
            (risk_record.risk_index, risk_record.duty_date),
        ).fetchone()
        if previous is not None and previous["source_revision"] == risk_record.source_revision:
            raise Conflict("同一来源修订已登记")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO risk_index_risk_records(risk_index,duty_date,index_value,source_revision,observed_at,"
                    "supersedes_risk_record_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        risk_record.risk_index,
                        risk_record.duty_date,
                        decimal_text(risk_record.index_value),
                        risk_record.source_revision,
                        risk_record.observed_at,
                        None if previous is None else previous["risk_record_id"],
                        actor_id,
                        self._now(),
                    ),
                )
                risk_record_id = int(cursor.lastrowid)
                self._audit(
                    "risk_record",
                    str(risk_record_id),
                    "risk_record.recorded",
                    actor_id,
                    {"risk_index": risk_record.risk_index, "duty_date": risk_record.duty_date},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("风险指数版本冲突") from exc
        return {"risk_record_id": risk_record_id, "risk_index": risk_record.risk_index, "duty_date": risk_record.duty_date}

    def risk_summary(self, risk_index: str, sessions: int = 20) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT q.duty_date,q.index_value FROM risk_index_risk_records q "
            "JOIN (SELECT duty_date,max(risk_record_id) risk_record_id FROM risk_index_risk_records "
            "WHERE risk_index=? GROUP BY duty_date) latest ON latest.risk_record_id=q.risk_record_id "
            "ORDER BY q.duty_date DESC LIMIT ?",
            (risk_index.upper(), sessions),
        ).fetchall()
        points = [RiskPoint(row["duty_date"], Decimal(row["index_value"])) for row in rows]
        if not points:
            raise NotFound("没有基准风险指数")
        streak = latest_streak(points)
        average = moving_average(points, min(5, len(points)))
        latest = max(points, key=lambda item: item.duty_date)
        return {
            "risk_index": risk_index.upper(),
            "latest": {"duty_date": latest.duty_date, "index_value": decimal_text(latest.close)},
            "latest_streak": None if streak is None else streak.as_dict(),
            "moving_average": None if average is None else decimal_text(average),
            "evidence_items": len(points),
        }

    def create_facility(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        facility = ResponseCenter.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO response_centers(center_id,name,kind,timezone,capacity_units,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        facility.center_id,
                        facility.name,
                        facility.kind,
                        facility.timezone,
                        decimal_text(facility.capacity_units),
                        self._now(),
                    ),
                )
                self._audit("facility", facility.center_id, "facility.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("设施编号已经存在") from exc
        return dict(raw)

    def create_route(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        route = RoadCorridor.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO road_corridors(corridor_id,origin_center_id,destination_center_id,response_resource_kind,hourly_capacity,"
                    "delay_basis_points,response_minutes,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        route.corridor_id,
                        route.origin_center_id,
                        route.destination_center_id,
                        route.response_resource_kind,
                        decimal_text(route.hourly_capacity),
                        route.delay_basis_points,
                        route.response_minutes,
                        self._now(),
                    ),
                )
                self._audit("route", route.corridor_id, "route.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("道路走廊编号冲突或设施不存在") from exc
        return self.route(route.corridor_id)

    def route(self, corridor_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM road_corridors WHERE corridor_id=?", (corridor_id,)).fetchone()
        if row is None:
            raise NotFound("道路走廊不存在")
        return dict(row)

    def announce_restriction(
        self,
        actor_id: str,
        corridor_id: str,
        starts_at: str,
        ends_at: str | None,
        capacity_percent: object,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "outage.write")
        self.route(corridor_id)
        try:
            start = parse_utc(starts_at, "starts_at")
            end = None if ends_at is None else parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end is not None and end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percentage = Decimal(str(capacity_percent))
        if percentage < 0 or percentage > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO corridor_restrictions(corridor_id,starts_at,ends_at,capacity_percent,reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (corridor_id, utc_text(start), None if end is None else utc_text(end), decimal_text(percentage), reason, actor_id, self._now()),
            )
            restriction_id = int(cursor.lastrowid)
            self._audit("route", corridor_id, "outage.announced", actor_id, {"restriction_id": restriction_id})
        return {"restriction_id": restriction_id, "corridor_id": corridor_id, "state": "announced"}

    def add_inventory_lot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "inventory.write")
        lot = ResponseResourceLot.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO response_resource_lots(response_resource_lot_id,center_id,response_resource_kind,grade,quantity_units,available_units,"
                    "unit_cost_cny,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot.response_resource_lot_id,
                        lot.center_id,
                        lot.response_resource_kind,
                        lot.grade,
                        decimal_text(lot.quantity_units),
                        decimal_text(lot.quantity_units),
                        decimal_text(lot.unit_cost_cny),
                        lot.received_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("inventory_lot", lot.response_resource_lot_id, "inventory.received", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("应急资源批次冲突或设施不存在") from exc
        return self.inventory_lot(lot.response_resource_lot_id)

    def inventory_lot(self, response_resource_lot_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM response_resource_lots WHERE response_resource_lot_id=?", (response_resource_lot_id,)).fetchone()
        if row is None:
            raise NotFound("应急资源批次不存在")
        return dict(row)

    def inventory_summary(self, center_id: str, response_resource_kind: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM response_resource_lots WHERE center_id=? AND response_resource_kind=? ORDER BY received_at,response_resource_lot_id",
            (center_id, response_resource_kind),
        ).fetchall()
        return {"center_id": center_id, "response_resource_kind": response_resource_kind, **weighted_inventory_cost(rows)}

    def submit_dispatch(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "dispatch_request.write")
        dispatch_request = DispatchRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM traffic_idempotency WHERE scope='dispatch_request' AND idempotency_key=?",
            (dispatch_request.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同调度申请内容")
            return json.loads(stored["response_json"])
        route = self.route(dispatch_request.corridor_id)
        if route["state"] != "active":
            raise InvalidState("道路走廊当前不可调度申请")
        response = {
            "dispatch_id": dispatch_request.dispatch_id,
            "corridor_id": dispatch_request.corridor_id,
            "state": "submitted",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO dispatch_requests(dispatch_id,corridor_id,incident_id,duty_date,requested_units,"
                    "priority,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        dispatch_request.dispatch_id,
                        dispatch_request.corridor_id,
                        dispatch_request.incident_id,
                        dispatch_request.duty_date,
                        decimal_text(dispatch_request.requested_units),
                        dispatch_request.priority,
                        dispatch_request.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO traffic_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('dispatch_request',?,?,?,?)",
                    (dispatch_request.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("dispatch_request", dispatch_request.dispatch_id, "dispatch_request.submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("调度申请编号或幂等键冲突") from exc
        return response

    def _capacity_for_date(self, route: sqlite3.Row, duty_date: str) -> Decimal:
        start = duty_date + "T00:00:00Z"
        end = duty_date + "T23:59:59Z"
        rows = self.connection.execute(
            "SELECT capacity_percent FROM corridor_restrictions WHERE corridor_id=? AND state IN ('announced','active') "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?) ORDER BY restriction_id",
            (route["corridor_id"], end, start),
        ).fetchall()
        percentages = [Decimal(row["capacity_percent"]) for row in rows]
        return effective_capacity(Decimal(route["hourly_capacity"]), percentages)

    def allocate(self, actor_id: str, corridor_id: str, duty_date: str) -> dict[str, Any]:
        self._require(actor_id, "allocation.run")
        route = self.connection.execute("SELECT * FROM road_corridors WHERE corridor_id=?", (corridor_id,)).fetchone()
        if route is None:
            raise NotFound("道路走廊不存在")
        dispatch_requests = self.connection.execute(
            "SELECT * FROM dispatch_requests WHERE corridor_id=? AND duty_date=? AND state='submitted' "
            "ORDER BY priority,submitted_at,dispatch_id",
            (corridor_id, duty_date),
        ).fetchall()
        if not dispatch_requests:
            raise InvalidState("没有待分配调度申请")
        requests = [
            AllocationRequest(
                row["dispatch_id"],
                Decimal(row["requested_units"]),
                int(row["priority"]),
                row["submitted_at"],
            )
            for row in dispatch_requests
        ]
        available = self._capacity_for_date(route, duty_date)
        input_value = [dict(row) for row in dispatch_requests]
        input_sha256 = digest({"route": dict(route), "dispatch_requests": input_value, "capacity": str(available)})
        result_rows = allocate_capacity(available, requests)
        result = {
            "corridor_id": corridor_id,
            "duty_date": duty_date,
            "available_units": decimal_text(available),
            "allocations": result_rows,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO dispatch_plans(corridor_id,duty_date,input_sha256,available_units,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (corridor_id, duty_date, input_sha256, decimal_text(available), canonical_json(result), actor_id, self._now()),
            )
            for item in result_rows:
                state = "allocated" if Decimal(item["allocated_units"]) > 0 else "cancelled"
                self.connection.execute(
                    "UPDATE dispatch_requests SET allocated_units=?,state=?,revision=revision+1 "
                    "WHERE dispatch_id=? AND state='submitted'",
                    (item["allocated_units"], state, item["dispatch_id"]),
                )
            plan_id = int(cursor.lastrowid)
            self._audit("route", corridor_id, "allocation.completed", actor_id, {"plan_id": plan_id})
        return {"plan_id": plan_id, **result}

    def dispatch_deployment(
        self,
        actor_id: str,
        deployment_id: str,
        dispatch_id: str,
        response_resource_lot_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "deployment.write")
        dispatch_request = self.connection.execute(
            "SELECT n.*,r.delay_basis_points,r.response_minutes,r.origin_center_id FROM dispatch_requests n "
            "JOIN road_corridors r ON r.corridor_id=n.corridor_id WHERE n.dispatch_id=?",
            (dispatch_id,),
        ).fetchone()
        if dispatch_request is None:
            raise NotFound("调度申请不存在")
        if dispatch_request["state"] != "allocated" or dispatch_request["revision"] != expected_revision:
            raise InvalidState("调度申请不是当前可资源到场版本")
        lot = self.connection.execute("SELECT * FROM response_resource_lots WHERE response_resource_lot_id=?", (response_resource_lot_id,)).fetchone()
        if lot is None:
            raise NotFound("应急资源批次不存在")
        allocated = Decimal(dispatch_request["allocated_units"])
        available = Decimal(lot["available_units"])
        if lot["center_id"] != dispatch_request["origin_center_id"] or lot["response_resource_kind"] != self.route(dispatch_request["corridor_id"])["response_resource_kind"]:
            raise Conflict("应急资源批次与道路走廊起点或电源类型不匹配")
        if available < allocated:
            raise Conflict("应急资源库存不足以完成分配")
        expected_delivery = delivered_after_loss(allocated, int(dispatch_request["delay_basis_points"]))
        departed_at = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE response_resource_lots SET available_units=?,revision=revision+1 WHERE response_resource_lot_id=? AND revision=?",
                (decimal_text(quantize_volume(available - allocated)), response_resource_lot_id, lot["revision"]),
            )
            self.connection.execute(
                "UPDATE dispatch_requests SET state='in_transit',revision=revision+1 WHERE dispatch_id=? AND revision=?",
                (dispatch_id, expected_revision),
            )
            self.connection.execute(
                "INSERT INTO deployments(deployment_id,dispatch_id,inventory_response_resource_lot_id,deployed_units,"
                "expected_arrived_units,departed_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    deployment_id,
                    dispatch_id,
                    response_resource_lot_id,
                    decimal_text(allocated),
                    decimal_text(expected_delivery),
                    departed_at,
                    actor_id,
                    departed_at,
                ),
            )
            self._audit("deployment", deployment_id, "deployment.dispatched", actor_id, {"dispatch_id": dispatch_id})
        return {
            "deployment_id": deployment_id,
            "state": "in_transit",
            "deployed_units": decimal_text(allocated),
            "expected_arrived_units": decimal_text(expected_delivery),
            "expected_arrival": utc_text(parse_utc(departed_at) + timedelta(hours=int(dispatch_request["response_minutes"]))),
        }

    def create_scenario(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "scenario.write")
        scenario = ResponseScenario.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO response_scenarios(scenario_id,name,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (scenario.scenario_id, scenario.name, definition, content_sha256, actor_id, self._now()),
                )
                self._audit("scenario", scenario.scenario_id, "scenario.created", actor_id, {"sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("情景编号或内容已经存在") from exc
        return {"scenario_id": scenario.scenario_id, "state": "draft", "sha256": content_sha256}

    def approve_scenario(self, actor_id: str, scenario_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "scenario.approve")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE response_scenarios SET state='approved',revision=revision+1 "
                "WHERE scenario_id=? AND state='draft' AND revision=?",
                (scenario_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("情景不是当前草稿版本")
            self._audit("scenario", scenario_id, "scenario.approved", actor_id, {})
        return {"scenario_id": scenario_id, "state": "approved", "revision": expected_revision + 1}

    def run_scenario(self, actor_id: str, scenario_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "scenario.run")
        row = self.connection.execute(
            "SELECT * FROM response_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFound("情景不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准情景可以运行")
        scenario = ResponseScenario.from_dict(json.loads(row["definition_json"]))
        index_row = self.connection.execute(
            "SELECT index_value FROM risk_index_risk_records WHERE duty_date<=? ORDER BY duty_date DESC,risk_record_id DESC LIMIT 1",
            (as_of_date,),
        ).fetchone()
        if index_row is None:
            raise InvalidState("截止日期没有可用风险指数")
        road_corridors = self.connection.execute("SELECT * FROM road_corridors WHERE state='active' ORDER BY corridor_id").fetchall()
        inventory = self.connection.execute(
            "SELECT center_id,response_resource_kind,sum(CAST(available_units AS REAL)) available_units "
            "FROM response_resource_lots GROUP BY center_id,response_resource_kind ORDER BY center_id,response_resource_kind"
        ).fetchall()
        input_value = {
            "scenario_sha256": row["content_sha256"],
            "as_of_date": as_of_date,
            "index": index_row["index_value"],
            "road_corridors": [dict(item) for item in road_corridors],
            "inventory": [dict(item) for item in inventory],
        }
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT run_id,result_json FROM response_scenario_runs WHERE scenario_id=? AND as_of_date=? AND input_sha256=?",
            (scenario_id, as_of_date, input_sha256),
        ).fetchone()
        if existing is not None:
            return {"run_id": existing["run_id"], **json.loads(existing["result_json"]), "replayed": True}
        result = scenario_projection(
            current_index=Decimal(index_row["index_value"]),
            risk_index_drop_percent=scenario.risk_index_drop_percent,
            road_corridors=road_corridors,
            inventory=inventory,
            route_capacity_changes=scenario.route_capacity_changes,
            demand_changes=scenario.demand_changes,
        )
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO response_scenario_runs(scenario_id,as_of_date,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (scenario_id, as_of_date, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self._audit("scenario", scenario_id, "scenario.executed", actor_id, {"run_id": run_id})
        return {"run_id": run_id, **result, "replayed": False}

    def _group_event(self, group_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO incident_group_events(group_id,event_type,actor_id,payload_json,created_at) VALUES(?,?,?,?,?)",
            (group_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def _new_group_id(self, intake_id: str) -> str:
        base = f"IG-{intake_id}"
        candidate = base
        suffix = 2
        while self.connection.execute(
            "SELECT 1 FROM incident_groups WHERE group_id=?", (candidate,)
        ).fetchone():
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def _surviving_group_id(self, group_id: str) -> str:
        current = group_id
        seen = {current}
        while True:
            row = self.connection.execute(
                "SELECT state,merged_into_group_id FROM incident_groups WHERE group_id=?", (current,)
            ).fetchone()
            if row is None:
                raise NotFound("案件组不存在")
            if row["state"] == "open":
                return current
            current = row["merged_into_group_id"]
            if current in seen:
                raise InvalidState("案件组合并链存在环")
            seen.add(current)

    def _surviving_group_of_intake(self, intake_id: str) -> str:
        row = self.connection.execute(
            "SELECT group_id FROM alarm_intakes WHERE intake_id=?", (intake_id,)
        ).fetchone()
        if row is None:
            raise NotFound("报警受理单不存在")
        return self._surviving_group_id(row["group_id"])

    def _group_family(self, group_id: str) -> tuple[str, list[str]]:
        """返回（存续案件组编号, 合并链上全部案件组编号），用于跨并案还原历史。"""
        root = self._surviving_group_id(group_id)
        rows = self.connection.execute(
            "SELECT group_id,merged_into_group_id FROM incident_groups"
        ).fetchall()
        merged_into = {row["group_id"]: row["merged_into_group_id"] for row in rows}
        family: list[str] = []
        for row in rows:
            current = row["group_id"]
            seen: set[str] = set()
            while current is not None and current != root and current not in seen:
                seen.add(current)
                current = merged_into.get(current)
            if current == root:
                family.append(row["group_id"])
        return root, sorted(family)

    def _active_dispatches(self, group_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = sorted(set(group_ids))
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"SELECT dispatch_id,incident_id,state,requested_units,allocated_units FROM dispatch_requests "
            f"WHERE incident_id IN ({marks}) AND state IN ('submitted','allocated','in_transit') "
            f"ORDER BY dispatch_id",
            tuple(ids),
        ).fetchall()
        return [dict(row) for row in rows]

    def _merge_groups(
        self,
        actor_id: str,
        surviving_id: str,
        absorbed_id: str,
        extra: Mapping[str, Any],
    ) -> str:
        """把 absorbed 案件组并入 surviving；已派出资源只登记随车清单，不做任何改动。"""
        if surviving_id == absorbed_id:
            raise InvalidState("两条报警已在同一案件组")
        carried = self._active_dispatches([absorbed_id])
        cursor = self.connection.execute(
            "UPDATE incident_groups SET state='absorbed',merged_into_group_id=? "
            "WHERE group_id=? AND state='open'",
            (surviving_id, absorbed_id),
        )
        if cursor.rowcount != 1:
            raise InvalidState("被并入的案件组不是存续状态")
        self.connection.execute(
            "UPDATE alarm_intakes SET group_id=? WHERE group_id=?", (surviving_id, absorbed_id)
        )
        self._group_event(
            surviving_id,
            "group.merged",
            actor_id,
            {"absorbed_group_id": absorbed_id, "carried_dispatches": carried, **dict(extra)},
        )
        return surviving_id

    def _supersede_same_group_candidates(self, now: str) -> None:
        rows = self.connection.execute(
            "SELECT candidate_id,intake_id,existing_intake_id FROM merge_candidates WHERE state='pending'"
        ).fetchall()
        for row in rows:
            left = self._surviving_group_of_intake(row["intake_id"])
            right = self._surviving_group_of_intake(row["existing_intake_id"])
            if left == right:
                self.connection.execute(
                    "UPDATE merge_candidates SET state='superseded',decided_at=? WHERE candidate_id=?",
                    (now, row["candidate_id"]),
                )

    def receive_alarm(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "alarm.write")
        report = AlarmReport.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM traffic_idempotency "
            "WHERE scope='alarm_intake' AND idempotency_key=?",
            (report.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同报警内容")
            return {**json.loads(stored["response_json"]), "replayed": True}
        occurred = parse_utc(report.occurred_at, "occurred_at")
        signal = AlarmSignal(
            location_standardized=standardize_location(report.location_text),
            occurred_at=occurred,
            vehicle_plates=report.vehicle_plates,
            contact_sha256=contact_digest(report.reporter_contact),
            name_sha256=name_digest(report.reporter_name),
        )
        if not signal.location_standardized:
            raise ValidationFailed("location_text 标准化后为空")
        now = self._now()
        window_start = utc_text(occurred - CANDIDATE_WINDOW)
        window_end = utc_text(occurred + CANDIDATE_WINDOW)
        try:
            with transaction(self.connection, immediate=True):
                group_id = self._new_group_id(report.intake_id)
                self.connection.execute(
                    "INSERT INTO incident_groups(group_id,state,created_by,created_at) VALUES(?,'open',?,?)",
                    (group_id, actor_id, now),
                )
                self.connection.execute(
                    "INSERT INTO alarm_intakes(intake_id,source_channel,reporter_name_masked,reporter_name_sha256,"
                    "contact_masked,contact_sha256,location_raw,location_standardized,occurred_at,reported_at,"
                    "vehicle_plates_json,narrative,group_id,idempotency_key,received_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        report.intake_id,
                        report.source_channel,
                        mask_name(report.reporter_name),
                        signal.name_sha256,
                        mask_contact(report.reporter_contact),
                        signal.contact_sha256,
                        report.location_text,
                        signal.location_standardized,
                        utc_text(occurred),
                        now,
                        canonical_json(list(report.vehicle_plates)),
                        report.narrative,
                        group_id,
                        report.idempotency_key,
                        actor_id,
                        now,
                    ),
                )
                self._group_event(group_id, "intake.attached", actor_id, {
                    "intake_id": report.intake_id,
                    "source_channel": report.source_channel,
                    "occurred_at": utc_text(occurred),
                    "reported_at": now,
                })
                rows = self.connection.execute(
                    "SELECT i.* FROM alarm_intakes i JOIN incident_groups g ON g.group_id=i.group_id "
                    "WHERE g.state='open' AND i.intake_id<>? AND i.occurred_at BETWEEN ? AND ? "
                    "ORDER BY i.intake_id",
                    (report.intake_id, window_start, window_end),
                ).fetchall()
                candidates: list[dict[str, Any]] = []
                components_by_id: dict[int, list[dict[str, Any]]] = {}
                for row in rows:
                    other = AlarmSignal(
                        location_standardized=row["location_standardized"],
                        occurred_at=parse_utc(row["occurred_at"], "occurred_at"),
                        vehicle_plates=tuple(json.loads(row["vehicle_plates_json"])),
                        contact_sha256=row["contact_sha256"],
                        name_sha256=row["reporter_name_sha256"],
                    )
                    result = score_signals(signal, other)
                    if result.confidence == "none":
                        continue
                    components = [component.as_dict() for component in result.components]
                    cursor = self.connection.execute(
                        "INSERT INTO merge_candidates(intake_id,existing_intake_id,score,confidence,"
                        "components_json,created_at) VALUES(?,?,?,?,?,?)",
                        (
                            report.intake_id,
                            row["intake_id"],
                            result.score,
                            result.confidence,
                            canonical_json(components),
                            now,
                        ),
                    )
                    candidate_id = int(cursor.lastrowid)
                    components_by_id[candidate_id] = components
                    candidates.append({
                        "candidate_id": candidate_id,
                        "existing_intake_id": row["intake_id"],
                        "score": result.score,
                        "confidence": result.confidence,
                        "state": "pending",
                    })
                    self._group_event(row["group_id"], "candidate.generated", actor_id, {
                        "candidate_id": candidate_id,
                        "intake_id": report.intake_id,
                        "existing_intake_id": row["intake_id"],
                        "score": result.score,
                        "confidence": result.confidence,
                        "components": components,
                    })
                merged_into: str | None = None
                automatic = [item for item in candidates if item["confidence"] == "auto"]
                if automatic:
                    best = max(automatic, key=lambda item: (item["score"], -item["candidate_id"]))
                    target = self._surviving_group_of_intake(best["existing_intake_id"])
                    merged_into = self._merge_groups(actor_id, target, group_id, {
                        "trigger": "auto",
                        "candidate_id": best["candidate_id"],
                        "score": best["score"],
                        "components": components_by_id[best["candidate_id"]],
                    })
                    self.connection.execute(
                        "UPDATE merge_candidates SET state='auto_confirmed',decided_at=? WHERE candidate_id=?",
                        (now, best["candidate_id"]),
                    )
                    best["state"] = "auto_confirmed"
                    self._supersede_same_group_candidates(now)
                response = {
                    "intake_id": report.intake_id,
                    "group_id": merged_into or group_id,
                    "state": "merged" if merged_into else "open",
                    "candidates": candidates,
                    "replayed": False,
                }
                self.connection.execute(
                    "INSERT INTO traffic_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('alarm_intake',?,?,?,?)",
                    (report.idempotency_key, request_digest, canonical_json(response), now),
                )
                self._audit("alarm_intake", report.intake_id, "alarm.received", actor_id, {
                    "group_id": response["group_id"],
                    "source_channel": report.source_channel,
                    "candidates": len(candidates),
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("报警受理单编号或幂等键冲突") from exc
        return response

    def _intake_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "intake_id": row["intake_id"],
            "source_channel": row["source_channel"],
            "reporter_name_masked": row["reporter_name_masked"],
            "contact_masked": row["contact_masked"],
            "location_raw": row["location_raw"],
            "location_standardized": row["location_standardized"],
            "occurred_at": row["occurred_at"],
            "reported_at": row["reported_at"],
            "vehicle_plates": json.loads(row["vehicle_plates_json"]),
            "narrative": row["narrative"],
            "group_id": row["group_id"],
            "received_by": row["received_by"],
            "created_at": row["created_at"],
        }

    def alarm_intake(self, actor_id: str, intake_id: str) -> dict[str, Any]:
        self._require(actor_id, "alarm.read")
        row = self.connection.execute(
            "SELECT * FROM alarm_intakes WHERE intake_id=?", (intake_id,)
        ).fetchone()
        if row is None:
            raise NotFound("报警受理单不存在")
        return self._intake_dict(row)

    def _candidate_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "candidate_id": row["candidate_id"],
            "intake_id": row["intake_id"],
            "existing_intake_id": row["existing_intake_id"],
            "score": row["score"],
            "confidence": row["confidence"],
            "components": json.loads(row["components_json"]),
            "state": row["state"],
            "decided_by": row["decided_by"],
            "decided_at": row["decided_at"],
            "decision_reason": row["decision_reason"],
            "created_at": row["created_at"],
        }

    def merge_candidates(self, actor_id: str, state: str = "pending") -> dict[str, Any]:
        self._require(actor_id, "alarm.read")
        if state not in {"pending", "auto_confirmed", "confirmed", "rejected", "superseded", "all"}:
            raise ValidationFailed("state 不是受支持的候选状态")
        if state == "all":
            rows = self.connection.execute(
                "SELECT * FROM merge_candidates ORDER BY candidate_id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM merge_candidates WHERE state=? ORDER BY candidate_id", (state,)
            ).fetchall()
        return {"candidates": [self._candidate_dict(row) for row in rows]}

    def intake_candidates(self, actor_id: str, intake_id: str) -> dict[str, Any]:
        self._require(actor_id, "alarm.read")
        rows = self.connection.execute(
            "SELECT * FROM merge_candidates WHERE intake_id=? OR existing_intake_id=? ORDER BY candidate_id",
            (intake_id, intake_id),
        ).fetchall()
        if not rows and self.connection.execute(
            "SELECT 1 FROM alarm_intakes WHERE intake_id=?", (intake_id,)
        ).fetchone() is None:
            raise NotFound("报警受理单不存在")
        return {"intake_id": intake_id, "candidates": [self._candidate_dict(row) for row in rows]}

    def decide_merge(self, actor_id: str, candidate_id: int, decision: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "merge.decide")
        if decision not in {"confirm", "reject"}:
            raise ValidationFailed("decision 必须是 confirm 或 reject")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("裁决理由不能为空")
        row = self.connection.execute(
            "SELECT * FROM merge_candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise NotFound("合并候选不存在")
        if row["state"] != "pending":
            raise InvalidState("合并候选已处理")
        now = self._now()
        with transaction(self.connection, immediate=True):
            if decision == "reject":
                self.connection.execute(
                    "UPDATE merge_candidates SET state='rejected',decided_by=?,decided_at=?,decision_reason=? "
                    "WHERE candidate_id=?",
                    (actor_id, now, reason.strip(), candidate_id),
                )
                group_id = self._surviving_group_of_intake(row["intake_id"])
                self._group_event(group_id, "candidate.rejected", actor_id, {
                    "candidate_id": candidate_id,
                    "intake_id": row["intake_id"],
                    "existing_intake_id": row["existing_intake_id"],
                    "score": row["score"],
                    "reason": reason.strip(),
                })
                self._audit("merge_candidate", str(candidate_id), "merge.rejected", actor_id, {
                    "reason": reason.strip(),
                })
                return {"candidate_id": candidate_id, "state": "rejected"}
            left = self._surviving_group_of_intake(row["intake_id"])
            right = self._surviving_group_of_intake(row["existing_intake_id"])
            groups = {
                item["group_id"]: item
                for item in self.connection.execute(
                    "SELECT group_id,created_at FROM incident_groups WHERE group_id IN (?,?)",
                    (left, right),
                ).fetchall()
            }
            surviving, absorbed = sorted(
                (left, right), key=lambda item: (groups[item]["created_at"], item)
            )
            components = json.loads(row["components_json"])
            self._merge_groups(actor_id, surviving, absorbed, {
                "trigger": "supervisor",
                "candidate_id": candidate_id,
                "score": row["score"],
                "components": components,
                "reason": reason.strip(),
            })
            self.connection.execute(
                "UPDATE merge_candidates SET state='confirmed',decided_by=?,decided_at=?,decision_reason=? "
                "WHERE candidate_id=?",
                (actor_id, now, reason.strip(), candidate_id),
            )
            self._supersede_same_group_candidates(now)
            self._audit("merge_candidate", str(candidate_id), "merge.confirmed", actor_id, {
                "surviving_group_id": surviving,
                "absorbed_group_id": absorbed,
            })
            return {
                "candidate_id": candidate_id,
                "state": "confirmed",
                "group_id": surviving,
                "absorbed_group_id": absorbed,
            }

    def split_group(
        self,
        actor_id: str,
        group_id: str,
        intake_ids: Iterable[str],
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "merge.split")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("拆分理由不能为空")
        if not isinstance(intake_ids, (list, tuple)) or not intake_ids:
            raise ValidationFailed("intake_ids 必须是非空数组")
        ids: list[str] = []
        for item in intake_ids:
            value = str(item).strip()
            if not value:
                raise ValidationFailed("intake_ids 元素不能为空")
            if value not in ids:
                ids.append(value)
        group = self.connection.execute(
            "SELECT * FROM incident_groups WHERE group_id=?", (group_id,)
        ).fetchone()
        if group is None:
            raise NotFound("案件组不存在")
        if group["state"] != "open":
            raise InvalidState("只有存续中的案件组可以拆分")
        members = [
            row["intake_id"]
            for row in self.connection.execute(
                "SELECT intake_id FROM alarm_intakes WHERE group_id=? ORDER BY intake_id", (group_id,)
            ).fetchall()
        ]
        unknown = [item for item in ids if item not in members]
        if unknown:
            raise ValidationFailed(f"报警 {unknown} 不在案件组 {group_id} 中")
        remaining = [item for item in members if item not in ids]
        if not remaining:
            raise ValidationFailed("拆分必须至少保留一条报警在原案件组")
        with transaction(self.connection, immediate=True):
            new_group_id = self._new_group_id(ids[0])
            self.connection.execute(
                "INSERT INTO incident_groups(group_id,state,created_by,created_at) VALUES(?,'open',?,?)",
                (new_group_id, actor_id, self._now()),
            )
            marks = ",".join("?" for _ in ids)
            self.connection.execute(
                f"UPDATE alarm_intakes SET group_id=? WHERE intake_id IN ({marks})",
                (new_group_id, *ids),
            )
            retained_dispatches = self._active_dispatches([group_id])
            self._group_event(group_id, "group.split_out", actor_id, {
                "intake_ids": ids,
                "new_group_id": new_group_id,
                "retained_intake_ids": remaining,
                "dispatches_retained": retained_dispatches,
                "reason": reason.strip(),
            })
            self._group_event(new_group_id, "group.split_in", actor_id, {
                "intake_ids": ids,
                "source_group_id": group_id,
                "reason": reason.strip(),
            })
            self._audit("incident_group", group_id, "group.split", actor_id, {
                "new_group_id": new_group_id,
                "intake_ids": ids,
                "reason": reason.strip(),
            })
        return {
            "group_id": group_id,
            "new_group_id": new_group_id,
            "moved_intake_ids": ids,
            "state": "split",
        }

    def group_detail(self, actor_id: str, group_id: str) -> dict[str, Any]:
        self._require(actor_id, "alarm.read")
        root, family = self._group_family(group_id)
        marks = ",".join("?" for _ in family)
        intakes = self.connection.execute(
            f"SELECT * FROM alarm_intakes WHERE group_id IN ({marks}) ORDER BY reported_at,intake_id",
            tuple(family),
        ).fetchall()
        pending = self.connection.execute(
            f"SELECT COUNT(*) AS total FROM merge_candidates c WHERE c.state='pending' AND ("
            f"c.intake_id IN (SELECT intake_id FROM alarm_intakes WHERE group_id IN ({marks})) OR "
            f"c.existing_intake_id IN (SELECT intake_id FROM alarm_intakes WHERE group_id IN ({marks})))",
            tuple(family) + tuple(family),
        ).fetchone()["total"]
        return {
            "group_id": root,
            "family": family,
            "intakes": [self._intake_dict(row) for row in intakes],
            "active_dispatches": self._active_dispatches(family),
            "pending_candidates": pending,
        }

    def group_timeline(self, actor_id: str, group_id: str) -> dict[str, Any]:
        self._require(actor_id, "alarm.read")
        root, family = self._group_family(group_id)
        marks = ",".join("?" for _ in family)
        rows = self.connection.execute(
            f"SELECT * FROM incident_group_events WHERE group_id IN ({marks}) ORDER BY event_id",
            tuple(family),
        ).fetchall()
        return {
            "group_id": root,
            "family": family,
            "events": [
                {
                    "event_id": row["event_id"],
                    "group_id": row["group_id"],
                    "event_type": row["event_type"],
                    "actor_id": row["actor_id"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM traffic_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
