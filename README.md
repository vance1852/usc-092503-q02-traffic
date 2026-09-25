# 道路交通事故快处与处罚协同服务

本项目是一套可离线运行的 Python 后台，用于事故受理、道路风险研判、警力与拖车调度、结构化证据复核、责任认定、处罚执行和审计追溯。系统把同一事故从报警到结案的关键状态保存在 SQLite 中，角色权限覆盖接警员、调度员、事故处理民警、复核人员和审计人员。

## 目录

- `src/traffic_dispatch/`：事故风险指数、快处中心、道路走廊、应急资源、调度申请和响应情景；报警受理、候选合并、值班长裁决与拆案溯源；
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

### 报警受理与候选合并

早高峰同一起追尾常被当事人、路人、巡逻车分别报警。接警台提交报警（`POST /alerts`）后，系统按四类证据两两评分并落库可解释候选：

1. **标准化地点**：文字归一化键、道路名与里程桩锚点（如 `北环高速 K23+500`）、可选经纬度距离；
2. **时间邻近**：报警声称的事发时间，±120 分钟为硬窗口；
3. **车辆线索**：车牌为强标识（描述文本中的车牌也会被抽取），车型/颜色为弱标识；
4. **人员信息**：联系电话为强标识，姓名为弱标识。

候选分三档：`auto`（地点、时间、强标识齐备）受理时自动并案；`review`（中置信度）进入值班长裁决队列（`GET /merge_candidates`、`POST /merge_candidates/{id}/decision`）；`reject` 不建议合并但仍留档。每条候选都带逐因子贡献分与中文判定依据。

关键不变式：

- **重复提交**：同幂等键重放返回原受理单（请求体不一致报 409）；来源、联系人电话、标准化地点、事发分钟、车牌集合一致的报警按内容指纹判重，直接返回首条原始报警，不新建案件；
- **原始报警保留**：每条报警独立存储，案件是合并视图；默认返回脱敏联系人（`张*`、`138****1234`），值班长持 `contact.read` 可用 `?unmask=1` 查看明文；
- **来源时间线**：`GET /reports/{id}/timeline` 还原接收、重复提交、并案、派警、拆分等节点及其发生时的案件归属；
- **已派资源不被并案取消**：并案只迁移资源归属，`cancelled_by_merge` 恒为 0；取消必须由有权人显式执行（`POST /alert_resources/{id}/cancel`）并填写原因；
- **错误合并可拆分**：`POST /reports/{id}/split` 把报警连同其资源移回新案件，处置历史与时间线全部保留；
- **后台溯源**：`GET /cases/{id}/lineage` 通过 merge/split 链接（分别关联合并候选与被撤销的合并）还原案件为何合并或分离。

相关接口：`POST /alerts`、`GET /merge_candidates`、`POST /merge_candidates/{id}/decision`、`POST /reports/{id}/split`、`POST /alert_resources`、`POST /alert_resources/{id}/cancel`、`GET /cases/{id}`、`GET /reports/{id}/timeline`、`GET /cases/{id}/lineage`。角色新增接警员 `calltaker` 与值班长 `supervisor`。
