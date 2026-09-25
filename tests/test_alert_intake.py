from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from traffic_dispatch.alerts import (
    AlertInput,
    Location,
    location_score,
    normalize_phone,
    normalize_plate,
    score_alert_pair,
)
from traffic_dispatch.api import JsonApplication
from traffic_dispatch.clock import FrozenClock
from traffic_dispatch.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from traffic_dispatch.service import TrafficDispatchService


def alert_payload(report_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "report_id": report_id,
        "idempotency_key": f"idem-{report_id}",
        "source": "caller",
        "source_ref": f"110-{report_id}",
        "location": {"text": "北环高速东行 K23+500"},
        "incident_kind": "collision",
        "description": "早高峰被后车追尾",
        "contacts": [{"name": "张伟", "phone": "138-0000-1234", "relation": "当事人"}],
        "vehicles": [{"plate": "粤B12345", "kind": "sedan", "color": "white"}],
        "occurred_at": "2026-09-25T00:30:00Z",
    }
    payload.update(overrides)
    return payload


# 无车牌的弱线索，用于构造需要值班长裁决的中置信度候选
WEAK_VEHICLE = [{"kind": "sedan", "color": "white"}]


class NormalizationTests(unittest.TestCase):
    def test_phone_and_plate_normalization(self) -> None:
        self.assertEqual(normalize_phone("+86 138-0000-1234"), "13800001234")
        self.assertEqual(normalize_phone("1234"), "")
        self.assertEqual(normalize_plate("粤 b12345"), "粤B12345")
        self.assertEqual(normalize_plate("无牌车"), "")

    def test_scoring_explains_factors_and_bands(self) -> None:
        strong = AlertInput.from_dict(alert_payload("p1"))
        other = AlertInput.from_dict(alert_payload("p2", source="witness", source_ref="110-p2"))
        same = score_alert_pair(strong, other)
        self.assertEqual(same.band, "auto")
        factors = {reason["factor"] for reason in same.reasons}
        self.assertIn("location", factors)
        self.assertIn("time", factors)
        self.assertIn("vehicle_plate", factors)

        distant = AlertInput.from_dict(alert_payload(
            "p3",
            location={"text": "南坪快速 K80+000"},
            contacts=[{"name": "李四", "phone": "13700007777"}],
            vehicles=[{"kind": "truck", "color": "blue"}],
            occurred_at="2026-09-25T03:30:00Z",
        ))
        weak = score_alert_pair(strong, distant)
        self.assertEqual(weak.band, "reject")
        self.assertFalse(weak.within_time_gate)

    def test_milepost_proximity_without_coordinates(self) -> None:
        a = Location.from_raw({"text": "北环高速 K23+500"})
        b = Location.from_raw({"text": "北环高速 K23+560 附近"})
        value, detail = location_score(a, b)
        self.assertGreaterEqual(value, Decimal("0.9"))
        self.assertIn("里程桩", detail)


class AlertIntakeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc))
        self.service = TrafficDispatchService(self.connection, self.clock)
        for user_id, role in (
            ("ct", "calltaker"),
            ("sup", "supervisor"),
            ("disp", "dispatcher"),
            ("au", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    def payload(self, report_id: str, **overrides: object) -> dict[str, object]:
        return alert_payload(report_id, **overrides)

    def test_duplicate_idempotency_returns_original_receipt(self) -> None:
        payload = self.payload("dup-1")
        first = self.service.accept_alert("ct", payload)
        second = self.service.accept_alert("ct", payload)
        self.assertEqual(first, second)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) n FROM alert_reports").fetchone()["n"], 1)
        changed = dict(payload)
        changed["description"] = "内容被篡改"
        with self.assertRaises(Conflict):
            self.service.accept_alert("ct", changed)

    def test_fingerprint_duplicate_returns_original_report(self) -> None:
        first = self.service.accept_alert("ct", self.payload("f1"))
        result = self.service.accept_alert("ct", self.payload("f2"))
        self.assertEqual(result["report_id"], "f1")
        self.assertEqual(result["case_id"], first["case_id"])
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["duplicate_of_report_id"], "f1")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) n FROM alert_reports").fetchone()["n"], 1)
        timeline = self.service.report_timeline("ct", "f1")
        self.assertIn("duplicate_submission", [event["event_type"] for event in timeline["events"]])

    def test_identical_multi_source_reports_auto_merge(self) -> None:
        first = self.service.accept_alert("ct", self.payload("m1"))
        second = self.service.accept_alert("ct", self.payload("m2", source="witness"))
        third = self.service.accept_alert("ct", self.payload("m3", source="patrol"))
        self.assertEqual({first["case_id"], second["case_id"], third["case_id"]}, {"m1"})
        case = self.service.case_detail("au", "m1")
        self.assertEqual([r["report_id"] for r in case["reports"]], ["m1", "m2", "m3"])
        self.assertEqual({r["source"] for r in case["reports"]}, {"caller", "witness", "patrol"})

    def test_low_confidence_goes_to_supervisor(self) -> None:
        self.service.accept_alert("ct", self.payload(
            "l1", vehicles=WEAK_VEHICLE, contacts=[{"name": "甲", "phone": "13800001111"}],
            location={"text": "北环高速 K23+500"}))
        self.service.accept_alert("ct", self.payload(
            "l2", source="witness", vehicles=[{"kind": "sedan", "color": "silver"}],
            contacts=[{"name": "乙", "phone": "13800002222"}],
            location={"text": "北环高速 K23+600 附近"},
            occurred_at="2026-09-25T00:40:00Z"))
        candidates = self.service.list_merge_candidates("ct")
        self.assertEqual(candidates["count"], 1)
        candidate = candidates["candidates"][0]
        self.assertEqual(candidate["band"], "review")
        self.assertEqual(candidate["state"], "proposed")
        with self.assertRaises(Forbidden):
            self.service.decide_merge_candidate("ct", candidate["candidate_id"], "accepted")
        decided = self.service.decide_merge_candidate("sup", candidate["candidate_id"], "accepted", "监控确认")
        self.assertEqual(decided["state"], "accepted")
        self.assertEqual(self.service.case_detail("ct", "l2")["case_id"], "l1")

    def test_rejected_candidate_keeps_separate_cases(self) -> None:
        self.service.accept_alert("ct", self.payload(
            "j1", vehicles=WEAK_VEHICLE, contacts=[{"name": "甲", "phone": "13800001111"}]))
        self.service.accept_alert("ct", self.payload(
            "j2", vehicles=[{"kind": "suv", "color": "black"}],
            contacts=[{"name": "乙", "phone": "13800002222"}],
            location={"text": "北环高速 K23+600"}, occurred_at="2026-09-25T00:42:00Z"))
        candidate_id = self.service.list_merge_candidates("ct")["candidates"][0]["candidate_id"]
        self.service.decide_merge_candidate("sup", candidate_id, "rejected", "车牌车型都对不上")
        self.assertEqual(self.service.case_detail("ct", "j1")["case_id"], "j1")
        self.assertEqual(self.service.case_detail("ct", "j2")["case_id"], "j2")
        lineage = self.service.case_lineage("au", "j1")
        self.assertEqual(lineage["other_decisions"][0]["state"], "rejected")

    def test_merge_never_cancels_dispatched_resources(self) -> None:
        self.service.accept_alert("ct", self.payload("r1"))
        self.service.accept_alert("ct", self.payload(
            "r2", vehicles=[{"kind": "sedan", "color": "silver"}],
            contacts=[{"name": "王五", "phone": "13800003333"}],
            location={"text": "北环高速 K23+520"},
            occurred_at="2026-09-25T00:35:00Z"))
        self.service.register_resource_dispatch("disp", {
            "resource_dispatch_id": "tow-1", "report_id": "r2",
            "resource_kind": "tow-truck", "resource_ref": "拖车3号"})
        candidate_id = self.service.list_merge_candidates("ct")["candidates"][0]["candidate_id"]
        self.service.decide_merge_candidate("sup", candidate_id, "accepted", "同一起")
        case = self.service.case_detail("disp", "r1")
        self.assertEqual(len(case["resources"]), 1)
        resource = case["resources"][0]
        self.assertEqual(resource["state"], "dispatched")
        self.assertEqual(resource["cancelled_by_merge"], 0)
        self.assertEqual(resource["resource_ref"], "拖车3号")

    def test_explicit_resource_cancel_is_audited_but_merge_cannot(self) -> None:
        self.service.accept_alert("ct", self.payload("x1"))
        self.service.register_resource_dispatch("disp", {
            "resource_dispatch_id": "patrol-1", "report_id": "x1",
            "resource_kind": "patrol-unit", "resource_ref": "巡逻7"})
        with self.assertRaises(ValidationFailed):
            self.service.cancel_resource_dispatch("disp", "patrol-1", "")
        self.service.cancel_resource_dispatch("disp", "patrol-1", "现场已撤离，显式召回")
        row = self.connection.execute(
            "SELECT cancelled_by_merge,state FROM alert_resource_dispatches").fetchone()
        self.assertEqual(row["state"], "cancelled")
        self.assertEqual(row["cancelled_by_merge"], 0)

    def test_split_preserves_history_and_resources(self) -> None:
        self.service.accept_alert("ct", self.payload("s1"))
        self.service.accept_alert("ct", self.payload(
            "s2", vehicles=[{"kind": "sedan", "color": "silver"}],
            contacts=[{"name": "赵六", "phone": "13800004444"}],
            location={"text": "北环高速 K23+520"},
            occurred_at="2026-09-25T00:34:00Z"))
        self.service.register_resource_dispatch("disp", {
            "resource_dispatch_id": "tow-s", "report_id": "s2",
            "resource_kind": "tow-truck", "resource_ref": "拖车9"})
        candidate_id = self.service.list_merge_candidates("ct")["candidates"][0]["candidate_id"]
        self.service.decide_merge_candidate("sup", candidate_id, "accepted", "先并")
        split = self.service.split_report("sup", "s2", "复核发现并非同一起事故")
        self.assertEqual(split["from_case_id"], "s1")
        kept = self.service.case_detail("ct", "s1")
        moved = self.service.case_detail("ct", split["case_id"])
        self.assertEqual([r["report_id"] for r in kept["reports"]], ["s1"])
        self.assertEqual([r["report_id"] for r in moved["reports"]], ["s2"])
        self.assertEqual(moved["resources"][0]["resource_dispatch_id"], "tow-s")
        self.assertEqual(moved["resources"][0]["state"], "dispatched")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) n FROM alert_reports").fetchone()["n"], 2)
        events = [e["event_type"] for e in self.service.report_timeline("au", "s2")["events"]]
        self.assertEqual(events, ["received", "resource_dispatched", "merged", "split"])

    def test_lineage_reconstructs_merge_and_split(self) -> None:
        self.service.accept_alert("ct", self.payload("g1"))
        self.service.accept_alert("ct", self.payload(
            "g2", vehicles=[{"kind": "sedan", "color": "silver"}],
            contacts=[{"name": "钱七", "phone": "13800005555"}],
            location={"text": "北环高速 K23+510"},
            occurred_at="2026-09-25T00:33:00Z"))
        candidate_id = self.service.list_merge_candidates("ct")["candidates"][0]["candidate_id"]
        self.service.decide_merge_candidate("sup", candidate_id, "accepted", "并案原因留档")
        self.service.split_report("sup", "g2", "拆案原因留档")
        lineage = self.service.case_lineage("au", "g1")
        reasons = {(link["link_type"], link["reason"]) for link in lineage["links"]}
        self.assertIn(("merge", "并案原因留档"), reasons)
        self.assertIn(("split", "拆案原因留档"), reasons)
        self.assertEqual(lineage["link_decisions"][0]["explanation"]["band"], "review")
        # 用已被吸收的旧案件 ID 查询，也能还原当前存续案件的链接
        old_lineage = self.service.case_lineage("au", "g2")
        self.assertEqual(old_lineage["case_id"], "g1")
        self.assertIn(("split", "拆案原因留档"),
                      {(link["link_type"], link["reason"]) for link in old_lineage["links"]})

    def test_contact_privacy_masked_by_default(self) -> None:
        self.service.accept_alert("ct", self.payload("p1"))
        masked = self.service.case_detail("ct", "p1")["reports"][0]["contacts"]
        self.assertEqual(masked[0]["phone"], "138****1234")
        self.assertEqual(masked[0]["name"], "张*")
        with self.assertRaises(Forbidden):
            self.service.case_detail("ct", "p1", unmask=True)
        unmasked = self.service.case_detail("sup", "p1", unmask=True)["reports"][0]["contacts"]
        self.assertEqual(unmasked[0]["phone"], "13800001234")
        self.assertEqual(unmasked[0]["name"], "张伟")


class AlertApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = TrafficDispatchService(
            self.connection, FrozenClock(datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc)))
        self.service.create_user("ct", "ct", "calltaker")
        self.app = JsonApplication(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def test_health_and_alert_route(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        payload = alert_payload("api-1")
        response = self.app.handle(
            "POST", "/alerts", {"X-Actor-Id": "ct"}, json.dumps(payload).encode())
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["report_id"], "api-1")


if __name__ == "__main__":
    unittest.main()
