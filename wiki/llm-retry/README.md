# 🔁 LLM Retry Budget & Counting

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> How Sherry counts provider retries: one physical attempt sequence per model call, a separate budget attempt, extra attempts that do not consume it, a per-turn fallback switch cap, and a timeout breaker that no longer survives into the next turn.

## The Counting Model

ZCode's adapter keeps `attempt` as a local variable of one model request. Sherry follows the same model: every `wrap_model_call` / `awrap_model_call` starts with `retry_count = 0`; a new model call is a new budget.

The wire event carries four numbers so a UI never has to guess:

| Field | Meaning |
| --- | --- |
| `attempt` | Physical request number, including extra attempts |
| `budget_attempt` | Physical attempt minus `extra_attempts` |
| `extra_attempts` | Auth-refresh / signature-repair attempts; they do not consume the budget |
| `max_attempts` | `1 + max_retries + extra_attempts` |

`retries = budget_attempt - 1`. With the defaults, `max_retries = 10`, so a normal turn shows `2/11`, ..., `11/11`.

## When Counters Reset

Only a new request path resets the budget. Additional attempts use arithmetic, not a reset.

| Event | Budget | Extra | Why |
| --- | --- | --- | --- |
| Model-call entry | reset | reset | Every model call gets its own `retry_count` |
| Fallback switch | reset | reset | New model, new request, new budget |
| Multimodal degradation | reset | reset | The request moved to the skill path |
| Content-filter rebind | reset | reset | The request moved to another model |
| Auth refresh | unchanged | +1 | Extra attempt; it must not consume `max_retries` |
| Signature repair | unchanged | +1 | Same rule |
| Empty completion | +1 | unchanged | It is a real retry; its own counter caps it at one |
| Success | discarded | discarded | The counters are local to the call |

A completion with no text, no tool calls and no usage consumes one ordinary retry and is additionally capped by `empty_completion_retries = 1`. This matches ZCode's `canRetryEmptyCompletion`, which checks both `retryCount < 1` and `attempt < maxAttempts`.

## Fallback Switches

A fallback candidate starts a new model request, so its retry budget starts at zero. Without a cap, a chain of N candidates could issue `11 × N` physical requests. Sherry therefore tracks `llm_fallback_switches` per turn and refuses a switch once `max_fallback_switches` (default `1`) is reached; `0` also disables a sticky candidate. The session's sticky fallback index is unchanged: later turns still start on the candidate that already won.

## Stale-Streak Scope

`llm_stale_streak` counts consecutive timeout-classified failures. With `stale_giveup_scope = "turn"` (default), `before_agent` clears it at every main-agent turn; the breaker still aborts the current turn at `stale_giveup_threshold = 5`. `"session"` restores the old cross-turn behavior. ZCode has no equivalent cross-turn breaker; this is deliberate Sherry protection, not a parity claim.

## Events, UI and the Ledger

Every scheduled / succeeded / exhausted transition records one `model_retry` event and pushes it to the session's sockets:

```text
{"event":"model_retry","session_id":…,"content":{
  "status":"scheduled","attempt":2,"budget_attempt":2,
  "max_attempts":11,"retries":1,"extra_attempts":0,
  "fallback_switches":0,"reason":"rate_limit","delay_ms":1000}}
```

The chat row renders `attempt/max_attempts` while the turn streams; the ledger stores each transition as its own row (the event id includes attempt and status). The frontend indicator is in-memory and disappears when the history is reloaded; the ledger is the durable record.

## What Is Not Implemented

- Auth-refresh and thinking-signature repair: no provider refresh seam exists in `models/LLMs` yet; the event fields are ready but no fake attempt is made.
- Off-peak queue rollback: ZCode's gateway contract is absent.
- `bounded` / `unbounded` / `single` budget modes: no production caller needs them yet.

## Code Map

- `agent/middlewares/llm_retry/core.py`: loop, counters, fallback cap, `before_agent`.
- `agent/middlewares/llm_retry/retry_events.py`: event payload and websocket push.
- `config/features/agent_side/llm_retry.py`: `LLM_RETRY` defaults.
- `runtime/session/state_keys.py`: `LLM_FALLBACK_SWITCHES`, `LLM_STALE_STREAK`.
- `client/app/composables/use-chat-stream.ts`: `retryStatus` mapping.
- `client/app/pages/home/components/ChatBox.vue`: the inline counter.
- `tests/agent/middlewares/test_llm_retry.py`: counting, fallback cap, stale scope.
