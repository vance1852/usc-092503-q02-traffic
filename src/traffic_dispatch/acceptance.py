"""贯通风险指数、道路走廊、应急资源库存、调度申请和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import TrafficDispatchService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = TrafficDispatchService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor"),
                          ("call", "calltaker"), ("super", "supervisor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_risk_record("plan", {"risk_index": "COLLISION", "duty_date": f"2026-09-{index}", "index_value": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"center_id": "center-east", "name": "北部事故快处中心", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_units": "500000"})
    service.create_facility("plan", {"center_id": "command-center-b", "name": "沿海终端", "kind": "command-center", "timezone": "Asia/Shanghai", "capacity_units": "800000"})
    service.create_route("plan", {"corridor_id": "corridor-east-1", "origin_center_id": "center-east", "destination_center_id": "command-center-b", "response_resource_kind": "patrol-unit", "hourly_capacity": "100000", "delay_basis_points": 25, "response_minutes": 36})
    service.add_inventory_lot("dispatch", {"response_resource_lot_id": "lot-001", "center_id": "center-east", "response_resource_kind": "patrol-unit", "grade": "COLLISION", "quantity_units": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_dispatch("dispatch", {"dispatch_id": "nom-001", "corridor_id": "corridor-east-1", "incident_id": "medical-center-east", "duty_date": "2026-09-25", "requested_units": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "corridor-east-1", "2026-09-25")
    deployment = service.dispatch_deployment("dispatch", "deployment-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "road-section-restart", "name": "主干路恢复通行与事故需求回落", "risk_index_drop_percent": "9", "route_capacity_changes": {"corridor-east-1": "20"}, "demand_changes": {"center-east:patrol-unit": "-5"}})
    service.approve_scenario("risk", "road-section-restart", 1)
    scenario = service.run_scenario("plan", "road-section-restart", "2026-09-23")

    # 早高峰同一起追尾：当事人、路人、巡逻车分别报警
    alert_base = {
        "incident_kind": "collision",
        "occurred_at": "2026-09-24T07:40:00Z",
    }
    caller_alert = {
        **alert_base,
        "report_id": "alert-001", "idempotency_key": "alert-key-001",
        "source": "caller", "source_ref": "110-24001",
        "location": {"text": "北环高速东行 K23+500"},
        "description": "早高峰被后车追尾，车辆无法移动",
        "contacts": [{"name": "张伟", "phone": "138-0000-1234", "relation": "当事人"}],
        "vehicles": [{"plate": "粤B12345", "kind": "sedan", "color": "white"}],
    }
    alert_one = service.accept_alert("call", caller_alert)
    alert_two = service.accept_alert("call", {
        **alert_base,
        "report_id": "alert-002", "idempotency_key": "alert-key-002",
        "source": "witness", "source_ref": "110-24002",
        "location": {"text": "北环高速 东行 K23+500 附近"},
        "description": "看到白车 粤B12345 被追尾",
        "contacts": [{"name": "路人甲", "phone": "139-0000-8888", "relation": "目击者"}],
        "vehicles": [],
    })
    alert_three = service.accept_alert("call", {
        **alert_base,
        "report_id": "alert-003", "idempotency_key": "alert-key-003",
        "source": "patrol", "source_ref": "patrol-car-07",
        "location": {"text": "北环高速 K23+505"},
        "description": "巡逻发现两车事故，占用最右车道",
        "contacts": [{"name": "巡逻七组", "phone": "12110", "relation": "巡逻车"}],
        "vehicles": [{"kind": "sedan", "color": "white"}],
    })
    # 重复报警：新受理请求但指纹一致，返回原受理单
    duplicate = service.accept_alert("call", {
        **caller_alert, "report_id": "alert-004", "idempotency_key": "alert-key-004",
    })
    # 巡逻车在裁决前已叫拖车，并案不得取消
    service.register_resource_dispatch("dispatch", {
        "resource_dispatch_id": "tow-alert-001", "report_id": "alert-003",
        "resource_kind": "tow-truck", "resource_ref": "拖车3号",
        "detail": {"requested_by": "巡逻七组"},
    })
    review_candidates = service.list_merge_candidates("super", "proposed")
    review_candidate = next(
        item for item in review_candidates["candidates"]
        if item["band"] == "review" and "alert-003" in {item["left_report_id"], item["right_report_id"]}
    )
    service.decide_merge_candidate(
        "super", review_candidate["candidate_id"], "accepted", "值班长核对监控，确认三起报警为同一事故"
    )
    merged_case = service.case_detail("call", "alert-001")
    # 复核后发现巡逻车上报的是后方另一起剐蹭，拆分但保留处置历史
    split = service.split_report("super", "alert-003", "巡逻车事故位于 K23+505 后方，系另一起剐蹭")
    alert_lineage = service.case_lineage("audit", "alert-001")
    result = {
        "status": "ok",
        "index": service.risk_summary("COLLISION"),
        "plan_id": allocation["plan_id"],
        "deployment": deployment,
        "scenario_run_id": scenario["run_id"],
        "alerts": {
            "first_receipt": alert_one,
            "auto_merged_receipt": alert_two,
            "review_receipt": alert_three,
            "duplicate_receipt": duplicate,
            "merged_case_reports": [item["report_id"] for item in merged_case["reports"]],
            "merged_resources": merged_case["resources"],
            "split": split,
            "lineage_links": [
                {"type": link["link_type"], "from": link["from_case_id"], "to": link["to_case_id"],
                 "reason": link["reason"]}
                for link in alert_lineage["links"]
            ],
        },
        "audit": service.audit_chain("audit"),
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行事故快处中心调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
