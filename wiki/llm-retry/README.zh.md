# 🔁 LLM 重试预算与计数

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> Sherry 如何统计模型重试：每次模型调用一条物理尝试序列、一个独立的预算计数、不占预算的额外尝试、每回合的 fallback 切换上限，以及不再跨回合残留的超时熔断。

## 计数模型

ZCode 的 adapter 把 `attempt` 当作单次模型请求的局部变量。Sherry 采用同一模型：每次 `wrap_model_call` / `awrap_model_call` 都从 `retry_count = 0` 开始；新的模型调用就是新的预算。

WS 事件携带四个数字，前端不需要自行推导：

| 字段 | 含义 |
| --- | --- |
| `attempt` | 物理请求序号，包含额外尝试 |
| `budget_attempt` | 物理尝试减去 `extra_attempts` |
| `extra_attempts` | 鉴权刷新 / 签名修复尝试；不消耗预算 |
| `max_attempts` | `1 + max_retries + extra_attempts` |

`retries = budget_attempt - 1`。默认 `max_retries = 10`，所以正常回合显示 `2/11`、……、`11/11`。

## 何时重置

只有换请求路径才重置预算；额外尝试用减法，不是重置。

| 事件 | 预算 | 额外 | 原因 |
| --- | --- | --- | --- |
| 模型调用入口 | 归零 | 归零 | 每次模型调用都有自己的 `retry_count` |
| fallback 切换 | 归零 | 归零 | 新模型、新请求、新预算 |
| 多模态降级 | 归零 | 归零 | 请求改走 skill 路径 |
| 内容过滤重绑 | 归零 | 归零 | 请求改走另一个模型 |
| 鉴权刷新 | 不变 | +1 | 额外尝试，不能消耗 `max_retries` |
| 签名修复 | 不变 | +1 | 同一规则 |
| 空补全 | +1 | 不变 | 它是一次真实重试，另有自己的上限 1 |
| 成功 | 丢弃 | 丢弃 | 计数器只属于本次调用 |

没有文本、没有 tool call、也没有 usage 的空补全会消耗一次普通重试，并额外受 `empty_completion_retries = 1` 限制。这与 ZCode 的 `canRetryEmptyCompletion` 一致：同时检查 `retryCount < 1` 和 `attempt < maxAttempts`。

## fallback 切换

fallback 候选会发起新的模型请求，所以它的重试预算从零开始。没有上限时，N 个候选可能发出 `11 × N` 次物理请求。Sherry 因此按回合记录 `llm_fallback_switches`，达到 `max_fallback_switches`（默认 `1`）后拒绝继续切换；`0` 同时禁用已经粘滞的候选。会话级的 fallback 索引不变：后续回合仍从已经胜出的候选开始。

## 超时熔断范围

`llm_stale_streak` 统计连续 timeout 失败。`stale_giveup_scope = "turn"`（默认）时，`before_agent` 在每个主 agent 回合开始时清零；熔断仍会在 `stale_giveup_threshold = 5` 时中止当前回合。`"session"` 恢复旧的跨回合行为。ZCode 没有等价的跨回合熔断；这是 Sherry 有意的保护，不是 parity 声明。

## 事件、UI 与账本

每次 scheduled / succeeded / exhausted 都会记录一条 `model_retry` 事件，并推送到会话的 socket：

```text
{"event":"model_retry","session_id":…,"content":{
  "status":"scheduled","attempt":2,"budget_attempt":2,
  "max_attempts":11,"retries":1,"extra_attempts":0,
  "fallback_switches":0,"reason":"rate_limit","delay_ms":1000}}
```

回合流式输出期间，聊天行显示 `attempt/max_attempts`；账本把每次转换存成独立一行（事件 id 含 attempt 和 status）。前端提示只在内存中，历史刷新后会消失；账本才是持久记录。

## 尚未实现

- 鉴权刷新与 thinking 签名修复：`models/LLMs` 目前没有 provider 刷新入口；事件字段已就绪，但不会伪造一次必然失败的尝试。
- off-peak 排队回退：缺少 ZCode 的网关契约。
- `bounded` / `unbounded` / `single` 预算档位：目前没有生产调用方需要。

## 代码地图

- `agent/middlewares/llm_retry/core.py`：循环、计数、fallback 上限、`before_agent`。
- `agent/middlewares/llm_retry/retry_events.py`：事件载荷与 WebSocket 推送。
- `config/features/agent_side/llm_retry.py`：`LLM_RETRY` 默认值。
- `runtime/session/state_keys.py`：`LLM_FALLBACK_SWITCHES`、`LLM_STALE_STREAK`。
- `client/app/composables/use-chat-stream.ts`：`retryStatus` 映射。
- `client/app/pages/home/components/ChatBox.vue`：行内重试提示。
- `tests/agent/middlewares/test_llm_retry.py`：计数、fallback 上限、熔断范围。
