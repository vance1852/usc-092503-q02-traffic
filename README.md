# 道路交通事故快处与处罚协同服务

本项目是一套可离线运行的 Python 后台，用于事故受理、道路风险研判、警力与拖车调度、结构化证据复核、责任认定、处罚执行和审计追溯。系统把同一事故从报警到结案的关键状态保存在 SQLite 中，角色权限覆盖接警员、调度员、事故处理民警、复核人员和审计人员。

## 目录

- `src/traffic_dispatch/`：事故风险指数、快处中心、道路走廊、应急资源、调度申请、响应情景，以及报警受理与候选合并；
- `src/evidence_review/`：采集设备、证据规范、结构化记录导入、一致性分析、复核租约和采信决定；
- `src/penalty_ops/`：事故案件、违法记录、风险告警、处置工单、处罚流转和审计；
- `fixtures/`：离线验收使用的证据规范与结构化事故记录；
- `tests/`：领域规则、事务边界、权限、HTTP API 和 CLI 验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅依赖 Python 标准库与 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m traffic_dispatch.acceptance --workspace .
PYTHONPATH=src python3 -m evidence_review.acceptance --workspace .
PYTHONPATH=src python3 -m penalty_ops.acceptance
```

验收会建立临时 SQLite 数据库，登记事故风险记录、快处中心、道路走廊和应急资源，完成调度与证据复核，并输出 JSON 结果。命令不会访问公网，也不需要额外数据库、队列或常驻服务。

## HTTP API

```bash
PYTHONPATH=src python3 -m traffic_dispatch.api --database traffic.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m evidence_review.api --database evidence.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m penalty_ops.api --database penalties.sqlite3 --host 127.0.0.1 --port 8082
```

三个服务均提供 `GET /health`，其余接口使用 JSON。SQLite 文件保存业务状态、幂等结果和审计记录，进程重启后可继续查询。

## 报警受理与候选合并

`traffic_dispatch` 服务新增接警能力，覆盖同一事故被当事人、路人和巡逻车重复报警的场景：

- `POST /alarms`：接警员（`intake` 角色）受理报警。按 `idempotency_key` 幂等，重复提交返回原受理单；联系人与姓名只保存掩码和哈希，不明文落库。
- 受理时按标准化地点、事发时间邻近、号牌线索和报案人信息逐维度打分，输出带中文解释的匹配组件；高置信度自动并案，中低置信度进入待裁决队列。
- `GET /merge_candidates?state=pending`：值班长（`supervisor` 角色）查看待裁决候选；`POST /merge_candidates/{id}/decide` 携带 `decision` 与裁决理由确认或驳回。
- `POST /incident_groups/{id}/split`：错误合并可带理由拆分，原案件组至少保留一条报警，处置历史不丢失。
- `GET /incident_groups/{id}` 与 `GET /incident_groups/{id}/timeline`：还原案件组为何合并或分离，包含每条原始报警的来源渠道、报案时间和逐维度评分；已派出的调度资源只登记随车清单，不会被静默取消。
