# 燃气管线泄漏检测与隔离协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8333`。

- `app.py`：参数、依赖和服务生命周期。
- `src/domain.py`：管段、传感值和来源记录校验。
- `src/rules.py`：泄漏评分、阀门顺序、修复、试压、恢复状态机。
- `src/repository.py`：SQLite、重复保护、乐观版本和审计链。
- `src/service.py`：角色权限和业务编排。
- `src/http_api.py`：JSON 接口与首页。
- `src/audit.py`：可校验的审计事件。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8333
python3 -m unittest discover -s tests -v
python3 tests/smoke_http.py   # 端到端冒烟（占用 8391 端口）
```

## 处理约定

- **同段合并**：巡线员、传感网、调度对同一管段的报警合并为一条未结案事件（`POST /api/items` 已存在未结案事件时返回 `200` 与既有事件），来源记录并存于 `sources`；读数按就高原则并入并写入 `source_comparison`。完全重复的来源上报幂等，不重复记录。
- **核验留痕**：`verify` 每次确认生成一条修订（`verifications`），保留来源引用（`source_ids`）与当时的记录版本（`item_version`），旧判断不覆盖。阀门状态与现场不一致时置 `valve_status_conflict`，需现场确认（`valve_status_confirmed`）方可解除。
- **隔离断点重试**：`isolate` 只下发阀门顺序并进入 `isolating`；每个阀门以 `valve_receipt`（`valve_id` + `command_id`）回执推进，全部关闭才转 `isolated`。失败的阀门用新 `command_id` 重发即可从断点续传，已关闭阀门不受影响；相同 `command_id` 的重复回执不重复执行。
- **恢复对账**：`restore` 按阀门回执、试压记录、危险解除记录（`clear_hazard`）三方对账，任一不符即转入 `pending`（待处理）并记录不符项，补齐后重新对账才能恢复供气；显式声明危险未解除一律拒绝。
- **重启连续**：状态、版本与审计哈希链持久化于 SQLite，重启后可继续处理；`GET /api/items/<id>/audit` 返回 `chain_valid` 校验结果，旧库自动迁移 `merge_key`。

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和审计查询。测试覆盖完整抢修流程、同段合并、阀门断点重试与重复回执、试压阈值、现场危险条件、对账待处理、权限、版本冲突、重启连续性与审计链防篡改。模型不替代 SCADA、管网水力计算或正式应急预案。
