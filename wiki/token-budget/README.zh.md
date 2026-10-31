# Session Token 预算

> 每会话 token 上限、跨 turn 用量累计与估算花费。

## 🎯 概述

会话可以带一个可选的 token **上限**（`StateKey.TOKEN_BUDGET_CAP`）。当
`TOKEN_BUDGET_USED >= cap` 时，`TokenBudget`
（`agent/middlewares/token_budget/core.py`）会用终止 `AIMessage` 短路下一次
model call，不再请求 provider。该 turn 以“请总结”的提示结束；操作者可以调高
上限或重置计数器。

中间件与 `IterationBudget` 并列在主链中，始终开启（不可关闭）。没有上限时
完全 pass-through。token 数来自 provider 的 `usage_metadata`，是准确的；花费
是估算值。

## ⚙️ 配置

- `PUT /sessions/budget` 设置上限（`null` 清除，`0` 表示不限）。turn 运行中
  的修改会 PARK 到 `TOKEN_BUDGET_CAP_PENDING`，在 turn 边界 promote——与
  thinking/model 控件相同。
- `GET /sessions/budget` 返回 `{cap, session_cap, default_cap, inherited, used, cost, limited, pending}`：
  `cap` 是下一回合真正生效的上限（含继承来的默认值），`session_cap` 是会话自己的
  选择（`null` 表示从未设置，`0` 表示显式不限），`inherited` 表示是否正在使用
  `default_cap`。
- 从未设置上限的会话继承 `sherry.jsonc` 的 `TOKEN_BUDGET_DEFAULT_CAP`
  （0 表示不限，出厂即如此）。会话自己的值优先，所以把会话清成 `0` 就是显式不限、
  不会被正的默认值重新套上；`null` 则表示恢复继承。该值每个进程只读一次
  （文件需重启生效），因为它位于每次模型调用的热路径上。
- `POST /sessions/budget/reset` 清空 `TOKEN_BUDGET_USED`、
  `TOKEN_BUDGET_COST`、`TOKEN_BUDGET_LIMITED`；上限保留。
- `TOKEN_BUDGET_USED` / `TOKEN_BUDGET_COST` 跨 turn 累计，双写 mem + DB
  寄存器，不会被每 turn 的 iteration budget 重置。`clear_session` 会随其他
  session state 一起删除。
- `budget_warning` 在 `MODEL_PRICING["budget_warn_threshold"]`（0.80）触发一次；
  `budget_limited` 在上限首次触达时触发一次；两者都是 best-effort WS 推送。

- **Token 预算** 是当前会话的右栏标签（`TokenBudgetPanel.vue`，右栏 →「当前会话」），编辑该会话的上限并重置用量。入口在上下文指标弹框底部；面板会说明上限是继承应用配置还是本会话单独设置，上限输入框留空保存即为不限，只有标签载荷缺少会话时才显示空状态。
## 🧮 价格查找

价格表在 `.models` 中由用户配置：

```jsonc
"MODEL_PRICING_TABLE": "{\"glm-5\": {\"input\": 0.5, \"output\": 1.5}, \"_default\": {\"input\": 0.5, \"output\": 1.5}}"
```

值是序列化成 JSON 字符串的 JSON OBJECT，这样扁平的 `.models` key/value 编辑器
仍可工作。已有文件缺失该表时，会在 import 时补上（模板提供默认条目）。

`TokenBudget._cost_delta` 按四层解析花费：

1. `usage_metadata` / `response_metadata` 中 provider 直返的 cost
   （`cost`、`total_cost`、`estimated_cost`，或 `prompt_cost` +
   `completion_cost`）——直接使用；
2. 否则由 `config/features/infra_side/model_pricing.py::resolve_model_pricing`
   查用户表（精确模型名，再试 `/` 后的裸名）；
3. 再查本地 litellm 缓存 `src/data/litellm_prices.json`（gitignored；用
   `uv run --no-sync python scripts/sync_litellm_prices.py` 刷新）；
4. 最后用表的 `_default`（表本身不可用时回退到 `input $0.5/M`、
   `output $1.5/M`）。

上限只看 token，不看这个估算。

## 🧮 每回合预算（主代理 + 子代理）

`TOKEN_BUDGET_TURN_CAP` 是当前回合的可选上限。`TokenBudget` 在 `before_agent`
时生成 turn id，并重置 `TOKEN_BUDGET_TURN_USED`、`TOKEN_BUDGET_TURN_MAIN_USED`、
`TOKEN_BUDGET_TURN_CHILD_USED`、`TOKEN_BUDGET_TURN_COST` 和
`TOKEN_BUDGET_TURN_LIMITED`。每个子代理在 spawn 时写入
`TOKEN_BUDGET_PARENT = {session_id, turn_id}`；`ChildTokenBudget` 把它的 token
计入根父会话的 SESSION 计数（始终），并在父代理仍处于同一回合时计入 turn 计数。
因此 session 的 `used/cost` 也包含所有子代理。

`GET /sessions/budget` 额外返回 `turn_cap`、`session_turn_cap`、
`default_turn_cap`、`inherited_turn`、`turn_used`、`turn_main_used`、
`turn_child_used`、`turn_cost`、`turn_limited`、`turn_pending`。会话未设置时
继承 `sherry.jsonc` 的 `TOKEN_BUDGET_DEFAULT_TURN_CAP`；`PUT /sessions/budget`
可同时传 `cap` 和 `turn_cap`（各自 `null` 清除、`0` 显式不限）。重置会清空
会话与回合计数，但保留两个上限。

## 🌐 HTTP API

| 方法 | 路径 | Body / query |
| :--- | :--- | :--- |
| `GET` | `/sessions/budget` | `?session_id=<sid>` |
| `PUT` | `/sessions/budget` | `{session_id, cap?: int \| null, turn_cap?: int \| null}` |
| `POST` | `/sessions/budget/reset` | `{session_id}` |

## 🧪 测试

`tests/agent/middlewares/test_token_budget.py`（短路、累计、provider cost、
warning/limited 事件）、`tests/server/test_token_budget_settings.py`（idle 写入、
busy park/promote、reset、清除 latch）、`tests/config/test_model_pricing.py`
（表加载、litellm/default 优先级、provider 前缀模型名）与
`tests/server/trigger/http/test_token_budget_api.py`（HTTP 设上限 → 中间件累计
→ 短路 → GET 汇报 → reset 的端到端链路）。
