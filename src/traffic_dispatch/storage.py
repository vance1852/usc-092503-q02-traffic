"""事故快处服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS traffic_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','risk','auditor','calltaker','supervisor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_index_risk_records (
    risk_record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    risk_index TEXT NOT NULL,
    duty_date TEXT NOT NULL,
    index_value TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    supersedes_risk_record_id INTEGER REFERENCES risk_index_risk_records(risk_record_id),
    recorded_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(risk_index, duty_date, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_risk_records_series
ON risk_index_risk_records(risk_index, duty_date, risk_record_id);

CREATE TABLE IF NOT EXISTS response_centers (
    center_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    timezone TEXT NOT NULL,
    capacity_units TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS road_corridors (
    corridor_id TEXT PRIMARY KEY,
    origin_center_id TEXT NOT NULL REFERENCES response_centers(center_id),
    destination_center_id TEXT NOT NULL REFERENCES response_centers(center_id),
    response_resource_kind TEXT NOT NULL,
    hourly_capacity TEXT NOT NULL,
    delay_basis_points INTEGER NOT NULL,
    response_minutes INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','retired')),
    created_at TEXT NOT NULL,
    CHECK(origin_center_id <> destination_center_id)
);

CREATE TABLE IF NOT EXISTS corridor_restrictions (
    restriction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    corridor_id TEXT NOT NULL REFERENCES road_corridors(corridor_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    capacity_percent TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'announced' CHECK(state IN ('announced','active','closed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outages_route_time
ON corridor_restrictions(corridor_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS response_resource_lots (
    response_resource_lot_id TEXT PRIMARY KEY,
    center_id TEXT NOT NULL REFERENCES response_centers(center_id),
    response_resource_kind TEXT NOT NULL,
    grade TEXT NOT NULL,
    quantity_units TEXT NOT NULL,
    available_units TEXT NOT NULL,
    unit_cost_cny TEXT NOT NULL,
    received_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inventory_available
ON response_resource_lots(center_id, response_resource_kind, received_at);

CREATE TABLE IF NOT EXISTS response_resource_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    response_resource_lot_id TEXT NOT NULL REFERENCES response_resource_lots(response_resource_lot_id),
    delta_units TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispatch_requests (
    dispatch_id TEXT PRIMARY KEY,
    corridor_id TEXT NOT NULL REFERENCES road_corridors(corridor_id),
    incident_id TEXT NOT NULL,
    duty_date TEXT NOT NULL,
    requested_units TEXT NOT NULL,
    allocated_units TEXT NOT NULL DEFAULT '0',
    arrived_units TEXT NOT NULL DEFAULT '0',
    priority INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','allocated','in_transit','delivered','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dispatch_requests_schedule
ON dispatch_requests(corridor_id, duty_date, priority, submitted_at);

CREATE TABLE IF NOT EXISTS dispatch_plans (
    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    corridor_id TEXT NOT NULL REFERENCES road_corridors(corridor_id),
    duty_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    available_units TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(corridor_id, duty_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS deployments (
    deployment_id TEXT PRIMARY KEY,
    dispatch_id TEXT NOT NULL UNIQUE REFERENCES dispatch_requests(dispatch_id),
    inventory_response_resource_lot_id TEXT NOT NULL REFERENCES response_resource_lots(response_resource_lot_id),
    deployed_units TEXT NOT NULL,
    expected_arrived_units TEXT NOT NULL,
    departed_at TEXT NOT NULL,
    arrived_at TEXT,
    state TEXT NOT NULL DEFAULT 'in_transit' CHECK(state IN ('in_transit','delivered','disputed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS response_scenarios (
    scenario_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved','retired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS response_scenario_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL REFERENCES response_scenarios(scenario_id),
    as_of_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(scenario_id, as_of_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS traffic_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS traffic_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_traffic_audit_entity
ON traffic_audit_events(entity_type, entity_id, event_id);

-- 报警受理：原始报警永不物理删除，案件是合并后的受理单
CREATE TABLE IF NOT EXISTS incident_cases (
    case_id TEXT PRIMARY KEY,
    incident_kind TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open'
        CHECK(state IN ('open','merged','split','closed')),
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS alert_reports (
    report_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    case_id TEXT NOT NULL REFERENCES incident_cases(case_id),
    source TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    incident_kind TEXT NOT NULL,
    location_text TEXT NOT NULL,
    location_key TEXT NOT NULL,
    lat TEXT,
    lng TEXT,
    description TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    contacts_json TEXT NOT NULL,
    vehicles_json TEXT NOT NULL,
    persons_json TEXT NOT NULL,
    plates_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    duplicate_of_report_id TEXT REFERENCES alert_reports(report_id),
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alert_reports_case
ON alert_reports(case_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_alert_reports_match
ON alert_reports(location_key, occurred_at);

-- 重复提交指纹：同一来源/联系人/地点/事发分钟/车牌集合的报警视为重复
CREATE TABLE IF NOT EXISTS alert_report_fingerprints (
    rowid_fingerprint INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT NOT NULL UNIQUE REFERENCES alert_reports(report_id),
    fingerprint_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alert_fingerprint_value
ON alert_report_fingerprints(fingerprint_sha256);

-- 合并候选：每条记录解释 left/right 两条原始报警为何被建议合并
CREATE TABLE IF NOT EXISTS alert_merge_candidates (
    candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
    left_report_id TEXT NOT NULL REFERENCES alert_reports(report_id),
    right_report_id TEXT NOT NULL REFERENCES alert_reports(report_id),
    left_case_id TEXT NOT NULL REFERENCES incident_cases(case_id),
    right_case_id TEXT NOT NULL REFERENCES incident_cases(case_id),
    score TEXT NOT NULL,
    band TEXT NOT NULL CHECK(band IN ('auto','review','reject')),
    explanation_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'proposed'
        CHECK(state IN ('proposed','accepted','rejected','superseded')),
    created_at TEXT NOT NULL,
    decided_by TEXT REFERENCES traffic_users(user_id),
    decided_at TEXT,
    decision_note TEXT NOT NULL DEFAULT '',
    UNIQUE(left_report_id, right_report_id)
);

CREATE INDEX IF NOT EXISTS idx_merge_candidates_state
ON alert_merge_candidates(state, band, score);

-- 案件拆分/合并链接：merge 把 from_case 并入 to_case；split 把报告还回独立案件
CREATE TABLE IF NOT EXISTS incident_case_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    link_type TEXT NOT NULL CHECK(link_type IN ('merge','split')),
    from_case_id TEXT NOT NULL REFERENCES incident_cases(case_id),
    to_case_id TEXT NOT NULL REFERENCES incident_cases(case_id),
    related_candidate_id INTEGER REFERENCES alert_merge_candidates(candidate_id),
    reverts_link_id INTEGER REFERENCES incident_case_links(link_id),
    reason TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_case_links_from
ON incident_case_links(from_case_id, link_id);

CREATE INDEX IF NOT EXISTS idx_case_links_to
ON incident_case_links(to_case_id, link_id);

-- 报警来源时间线：接收、转单、并案、拆案等关键节点均留痕
CREATE TABLE IF NOT EXISTS alert_timeline_events (
    timeline_id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT NOT NULL REFERENCES alert_reports(report_id),
    case_id TEXT NOT NULL REFERENCES incident_cases(case_id),
    event_type TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    actor_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alert_timeline_report
ON alert_timeline_events(report_id, timeline_id);

-- 已派出的资源（警力/拖车）与原始报警的关联：并案/拆案不得删除或取消这些记录
CREATE TABLE IF NOT EXISTS alert_resource_dispatches (
    resource_dispatch_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES alert_reports(report_id),
    case_id TEXT NOT NULL REFERENCES incident_cases(case_id),
    resource_kind TEXT NOT NULL,
    resource_ref TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'dispatched'
        CHECK(state IN ('dispatched','arrived','released','cancelled')),
    cancelled_by_merge INTEGER NOT NULL DEFAULT 0 CHECK(cancelled_by_merge IN (0,1)),
    detail_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES traffic_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alert_resources_case
ON alert_resource_dispatches(case_id, state);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # HTTP 服务使用线程服务器，写事务均以 BEGIN IMMEDIATE 串行化
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
