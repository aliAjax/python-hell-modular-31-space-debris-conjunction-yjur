# 太空碎片接近预警与规避协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8331`。

## 模块

- `app.py`：参数解析、依赖组装和 HTTP 生命周期。
- `src/domain.py`：领域类型、校验和错误定义。
- `src/rules.py`：风险评估、意见冲突和状态机。
- `src/repository.py`：SQLite、事务、乐观版本和审计链。
- `src/service.py`：身份、权限、用例编排。
- `src/http_api.py`：JSON API 和静态首页。
- `src/audit.py`：哈希审计事件。

## 运行

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8331
```

服务提供 `GET /health`、`GET /api/state`、`GET /api/items`、`POST /api/items`、`POST /api/items/<id>/sources` 和 `POST /api/items/<id>/actions`。身份使用 `X-User-Id`、`X-Role` 请求头。

## 依据版本链

来源记录、评估结论和协调动作共用同一条版本链（item `version` 与审计哈希链）。

- **按观测时刻选当前依据**：新来源的 `observed_at` 新于当前依据时才成为 `current` 并重算评估；晚到的旧观测只保留为 `history`，评估数值和已批准动作不变。创建事件时录入的轨道参数是第 0 版依据，来源记录从 1 开始。
- **同时提交只形成一份当前依据**：两个站在同一 `observed_at` 提交时，先落库的为 `current`，另一份为 `pending`；必须由协调员通过 `confirm_source` 显式确认后才会切换依据（确认时其余 pending 记录降为历史）。
- 每条评估（`assessment`）和每个已批准规避动作（`approved_maneuver`）都冻结其依据快照：`basis_version` / `basis_source_id` / `observed_at`。
- **依据变化使未执行的批准失效**：处于 `coordinating`（已批准、指令尚未发出）时，批准移入 `payload.blocked_actions` 并附带新旧依据版本，事件回到 `assessed` 等待重新评估/批准；关联的未发完通知计划同时置为 `superseded`。已发出的执行（`executing`/`resolved`）通过 `executed_basis` 保留原依据，不受后续依据变化影响。
- 来源提交也推进 item 版本，可带 `expected_version` 做乐观并发控制。

## 通知派发

批准在同一事务中生成通知计划（`notification_plans`），每个运营方一个持久化步骤（`notification_steps`）。

- `POST /api/plans/<id>/dispatch`（协调员或运营方）：只领取未完成步骤（`pending`/`failed`/崩溃遗留的 `sending`），已 `sent` 的步骤永不重复下发。
- 每个步骤携带稳定的 `idempotency_key`，即使“已发送、未落库”崩溃，重启后重试点也携带相同键，接收侧据此去重。
- 单步失败不影响其他步骤；再次调用 dispatch 即接着原进度继续，只重试未完成步骤。重启进程后同样从持久化进度恢复。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖评估、批准、执行、解决、重复告警、权限、版本冲突、过期轨道、运营方意见冲突、依据版本链、晚到旧记录、同时提交待确认/确认、批准失效与受阻动作、已执行指令保留原依据，以及通知分步派发的失败重试、幂等不重复和崩溃重启续跑。数据使用 SQLite 持久化；规则是可运行的演示模型，不替代真实轨道力学、碰撞概率和空间交通协调服务。
