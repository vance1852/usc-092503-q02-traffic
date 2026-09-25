from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from traffic_dispatch.api import JsonApplication
from traffic_dispatch.clock import FrozenClock
from traffic_dispatch.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from traffic_dispatch.intake import (
    AlarmSignal,
    mask_contact,
    mask_name,
    normalize_plate,
    score_signals,
    standardize_location,
)
from traffic_dispatch.service import TrafficDispatchService


class IntakePureTests(unittest.TestCase):
    def test_location_standardization_unifies_synonyms_and_punctuation(self) -> None:
        self.assertEqual(
            standardize_location("G2 京沪高速公路 120公里处"),
            standardize_location("g2京沪高速120km"),
        )
        self.assertEqual(standardize_location("中山北路（近共和新路）"), "中山北路近共和新路")

    def test_plate_normalization_keeps_chinese_prefix(self) -> None:
        self.assertEqual(normalize_plate("沪a·d12345"), "沪AD12345")
        self.assertEqual(normalize_plate(" 苏E-8888 "), "苏E8888")

    def test_masks_hide_raw_contact_and_name(self) -> None:
        self.assertEqual(mask_contact("13812345678"), "138****5678")
        self.assertEqual(mask_contact("110"), "***")
        self.assertEqual(mask_name("王小明"), "王**")

    def test_high_confidence_scores_auto_with_explainable_components(self) -> None:
        occurred = datetime(2026, 9, 25, 7, 30, tzinfo=timezone.utc)
        first = AlarmSignal("g2高速120km", occurred, ("沪AD12345",), "hash-a", "name-a")
        second = AlarmSignal("g2高速120km", datetime(2026, 9, 25, 7, 33, tzinfo=timezone.utc), ("沪AD12345",), "hash-b", "name-b")
        result = score_signals(first, second)
        self.assertEqual(result.confidence, "auto")
        self.assertEqual(result.score, 95)
        dimensions = {component.dimension for component in result.components}
        self.assertEqual(dimensions, {"location", "time", "vehicle", "contact", "person"})
        self.assertTrue(all(component.detail for component in result.components))

    def test_review_and_none_thresholds(self) -> None:
        base = datetime(2026, 9, 25, 7, 30, tzinfo=timezone.utc)
        first = AlarmSignal("中山北路近共和新路", base, (), "hash-a", None)
        near = AlarmSignal("中山北路近共和新路", datetime(2026, 9, 25, 7, 50, tzinfo=timezone.utc), (), "hash-b", None)
        self.assertEqual(score_signals(first, near).confidence, "review")
        far = AlarmSignal("世纪大道近陆家嘴环路", base, (), "hash-c", None)
        self.assertEqual(score_signals(first, far).confidence, "none")


class AlarmIntakeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 25, 7, 40, tzinfo=timezone.utc))
        self.service = TrafficDispatchService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"),
            ("dispatch", "dispatcher"),
            ("intake-1", "intake"),
            ("leader", "supervisor"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"center_id": "center-east", "name": "北部事故快处中心", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_units": "500000"})
        self.service.create_facility("plan", {"center_id": "command-center-b", "name": "沿海终端", "kind": "command-center", "timezone": "Asia/Shanghai", "capacity_units": "800000"})
        self.service.create_route("plan", {"corridor_id": "corridor-east-1", "origin_center_id": "center-east", "destination_center_id": "command-center-b", "response_resource_kind": "patrol-unit", "hourly_capacity": "100000", "delay_basis_points": 25, "response_minutes": 36})

    def tearDown(self) -> None:
        self.connection.close()

    def alarm(self, intake_id: str, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "intake_id": intake_id,
            "source_channel": "party",
            "reporter_name": "王小明",
            "reporter_contact": "138-1234-5678",
            "location_text": "G2京沪高速公路120公里处",
            "occurred_at": "2026-09-25T07:30:00Z",
            "vehicle_plates": ["沪A·D12345"],
            "narrative": "两车追尾，占用最左侧车道",
            "idempotency_key": f"key-{intake_id}",
        }
        payload.update(overrides)
        return payload

    def test_repeated_submission_returns_original_ticket(self) -> None:
        first = self.service.receive_alarm("intake-1", self.alarm("alarm-1"))
        replay = self.service.receive_alarm("intake-1", self.alarm("alarm-1"))
        self.assertFalse(first.get("replayed", False))
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["intake_id"], replay["intake_id"])
        self.assertEqual(first["group_id"], replay["group_id"])
        changed = self.alarm("alarm-1", narrative="另一事故")
        with self.assertRaises(Conflict):
            self.service.receive_alarm("intake-1", changed)

    def test_high_confidence_alarm_auto_merges_and_preserves_originals(self) -> None:
        first = self.service.receive_alarm("intake-1", self.alarm("alarm-1"))
        second = self.service.receive_alarm("intake-1", self.alarm(
            "alarm-2",
            source_channel="patrol",
            reporter_name="李巡逻",
            reporter_contact="139-0000-1111",
            location_text="g2京沪高速120km",
            occurred_at="2026-09-25T07:33:00Z",
            vehicle_plates=["沪AD12345"],
            narrative="巡逻车到场前报：同一点位追尾",
        ))
        self.assertEqual(second["state"], "merged")
        self.assertEqual(second["group_id"], first["group_id"])
        detail = self.service.group_detail("leader", first["group_id"])
        self.assertEqual([row["intake_id"] for row in detail["intakes"]], ["alarm-1", "alarm-2"])
        channels = {row["intake_id"]: row["source_channel"] for row in detail["intakes"]}
        self.assertEqual(channels, {"alarm-1": "party", "alarm-2": "patrol"})
        timeline = self.service.group_timeline("audit", first["group_id"])
        merged = [event for event in timeline["events"] if event["event_type"] == "group.merged"]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["payload"]["trigger"], "auto")
        dimensions = {item["dimension"] for item in merged[0]["payload"]["components"]}
        self.assertIn("location", dimensions)
        self.assertIn("vehicle", dimensions)

    def test_low_confidence_candidate_waits_for_supervisor(self) -> None:
        first = self.service.receive_alarm("intake-1", self.alarm("alarm-1", vehicle_plates=[]))
        second = self.service.receive_alarm("intake-1", self.alarm(
            "alarm-2",
            source_channel="witness",
            reporter_name="赵路人",
            reporter_contact="137-9999-0000",
            occurred_at="2026-09-25T07:50:00Z",
            vehicle_plates=[],
            narrative="路人报警：同一路段有追尾",
        ))
        self.assertEqual(second["state"], "open")
        self.assertNotEqual(second["group_id"], first["group_id"])
        pending = self.service.merge_candidates("leader", "pending")["candidates"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["confidence"], "review")
        self.assertTrue(all(component["detail"] for component in pending[0]["components"]))
        with self.assertRaises(Forbidden):
            self.service.decide_merge("intake-1", pending[0]["candidate_id"], "confirm", "接警员无权裁决")
        decision = self.service.decide_merge("leader", pending[0]["candidate_id"], "confirm", "同一地点二十分钟内重复报警，确认为同一事故")
        self.assertEqual(decision["group_id"], first["group_id"])
        detail = self.service.group_detail("leader", first["group_id"])
        self.assertEqual(len(detail["intakes"]), 2)
        with self.assertRaises(InvalidState):
            self.service.decide_merge("leader", pending[0]["candidate_id"], "confirm", "重复裁决")

    def test_rejected_candidate_keeps_groups_separate_and_explainable(self) -> None:
        first = self.service.receive_alarm("intake-1", self.alarm("alarm-1", vehicle_plates=[]))
        second = self.service.receive_alarm("intake-1", self.alarm(
            "alarm-2",
            reporter_name="赵路人",
            reporter_contact="137-9999-0000",
            occurred_at="2026-09-25T07:45:00Z",
            vehicle_plates=[],
        ))
        candidate = self.service.merge_candidates("leader", "pending")["candidates"][0]
        result = self.service.decide_merge("leader", candidate["candidate_id"], "reject", "电话核实为相邻公里桩的两起事故")
        self.assertEqual(result["state"], "rejected")
        self.assertNotEqual(
            self.service.alarm_intake("leader", "alarm-1")["group_id"],
            self.service.alarm_intake("leader", "alarm-2")["group_id"],
        )
        timeline = self.service.group_timeline("audit", second["group_id"])
        rejected = [event for event in timeline["events"] if event["event_type"] == "candidate.rejected"]
        self.assertEqual(rejected[0]["payload"]["reason"], "电话核实为相邻公里桩的两起事故")
        self.assertEqual(first["group_id"], self.service.alarm_intake("leader", "alarm-1")["group_id"])

    def test_dispatched_resources_are_carried_not_cancelled(self) -> None:
        first = self.service.receive_alarm("intake-1", self.alarm("alarm-1", vehicle_plates=[]))
        second = self.service.receive_alarm("intake-1", self.alarm(
            "alarm-2",
            reporter_name="赵路人",
            reporter_contact="137-9999-0000",
            occurred_at="2026-09-25T07:44:00Z",
            vehicle_plates=[],
        ))
        self.service.submit_dispatch("dispatch", {
            "dispatch_id": "nom-alarm-2",
            "corridor_id": "corridor-east-1",
            "incident_id": second["group_id"],
            "duty_date": "2026-09-25",
            "requested_units": "20000",
            "priority": 5,
            "idempotency_key": "key-nom-alarm-2",
        })
        candidate = self.service.merge_candidates("leader", "pending")["candidates"][0]
        decision = self.service.decide_merge("leader", candidate["candidate_id"], "confirm", "确认为同一事故")
        dispatch_row = self.connection.execute(
            "SELECT state,incident_id FROM dispatch_requests WHERE dispatch_id='nom-alarm-2'"
        ).fetchone()
        self.assertEqual(dispatch_row["state"], "submitted")
        self.assertEqual(dispatch_row["incident_id"], second["group_id"])
        timeline = self.service.group_timeline("audit", decision["group_id"])
        merged = [event for event in timeline["events"] if event["event_type"] == "group.merged"][0]
        carried = merged["payload"]["carried_dispatches"]
        self.assertEqual([item["dispatch_id"] for item in carried], ["nom-alarm-2"])
        detail = self.service.group_detail("dispatch", decision["group_id"])
        self.assertEqual([item["dispatch_id"] for item in detail["active_dispatches"]], ["nom-alarm-2"])

    def test_wrong_merge_can_be_split_without_losing_history(self) -> None:
        first = self.service.receive_alarm("intake-1", self.alarm("alarm-1"))
        self.service.receive_alarm("intake-1", self.alarm(
            "alarm-2",
            source_channel="witness",
            reporter_name="赵路人",
            reporter_contact="137-9999-0000",
            occurred_at="2026-09-25T07:32:00Z",
        ))
        merged_group = first["group_id"]
        self.assertEqual(self.service.alarm_intake("leader", "alarm-2")["group_id"], merged_group)
        with self.assertRaises(Forbidden):
            self.service.split_group("intake-1", merged_group, ["alarm-2"], "接警员无权拆分")
        with self.assertRaises(ValidationFailed):
            self.service.split_group("leader", merged_group, ["alarm-1", "alarm-2"], "不能拆空原案件组")
        result = self.service.split_group("leader", merged_group, ["alarm-2"], "复核确认为两起事故，拆分恢复")
        new_group = result["new_group_id"]
        self.assertEqual(self.service.alarm_intake("leader", "alarm-2")["group_id"], new_group)
        self.assertEqual(self.service.alarm_intake("leader", "alarm-1")["group_id"], merged_group)
        source_timeline = self.service.group_timeline("audit", merged_group)
        self.assertEqual(source_timeline["events"][-1]["event_type"], "group.split_out")
        self.assertEqual(source_timeline["events"][-1]["payload"]["reason"], "复核确认为两起事故，拆分恢复")
        types = [event["event_type"] for event in source_timeline["events"]]
        self.assertIn("group.merged", types)
        new_timeline = self.service.group_timeline("audit", new_group)
        self.assertEqual(new_timeline["events"][0]["event_type"], "group.split_in")
        self.assertEqual(new_timeline["events"][0]["payload"]["source_group_id"], merged_group)

    def test_contact_privacy_is_masked_everywhere(self) -> None:
        first = self.service.receive_alarm("intake-1", self.alarm("alarm-1"))
        raw_fragments = ("13812345678", "138-1234-5678", "王小明")
        detail = json.dumps(self.service.alarm_intake("audit", "alarm-1"), ensure_ascii=False)
        timeline = json.dumps(self.service.group_timeline("audit", first["group_id"]), ensure_ascii=False)
        group = json.dumps(self.service.group_detail("audit", first["group_id"]), ensure_ascii=False)
        for fragment in raw_fragments:
            self.assertNotIn(fragment, detail + timeline + group)
        self.assertIn("138****5678", detail)
        self.assertIn("王**", detail)

    def test_receive_alarm_requires_intake_role(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.receive_alarm("dispatch", self.alarm("alarm-1"))
        with self.assertRaises(NotFound):
            self.service.alarm_intake("leader", "alarm-missing")

    def test_api_exposes_alarm_merge_boundary(self) -> None:
        app = JsonApplication(self.service)
        created = app.handle("POST", "/alarms", {"X-Actor-Id": "intake-1"}, json.dumps(self.alarm("alarm-1")).encode())
        self.assertEqual(created.status, 201)
        replay = app.handle("POST", "/alarms", {"X-Actor-Id": "intake-1"}, json.dumps(self.alarm("alarm-1")).encode())
        self.assertEqual(replay.status, 201)
        self.assertTrue(replay.body["replayed"])
        second = app.handle("POST", "/alarms", {"X-Actor-Id": "intake-1"}, json.dumps(self.alarm(
            "alarm-2",
            reporter_contact="137-9999-0000",
            occurred_at="2026-09-25T07:48:00Z",
            vehicle_plates=[],
        )).encode())
        self.assertEqual(second.status, 201)
        queue = app.handle("GET", "/merge_candidates?state=pending", {"X-Actor-Id": "leader"})
        self.assertEqual(queue.status, 200)
        candidate_id = queue.body["candidates"][0]["candidate_id"]
        decided = app.handle(
            "POST",
            f"/merge_candidates/{candidate_id}/decide",
            {"X-Actor-Id": "leader"},
            json.dumps({"decision": "confirm", "reason": "同一事故"}).encode(),
        )
        self.assertEqual(decided.status, 200)
        timeline = app.handle("GET", f"/incident_groups/{decided.body['group_id']}/timeline", {"X-Actor-Id": "audit"})
        self.assertEqual(timeline.status, 200)
        self.assertTrue(any(event["event_type"] == "group.merged" for event in timeline.body["events"]))
        forbidden = app.handle(
            "POST",
            f"/incident_groups/{decided.body['group_id']}/split",
            {"X-Actor-Id": "dispatch"},
            json.dumps({"intake_ids": ["alarm-2"], "reason": "越权"}).encode(),
        )
        self.assertEqual(forbidden.status, 403)


if __name__ == "__main__":
    unittest.main()
