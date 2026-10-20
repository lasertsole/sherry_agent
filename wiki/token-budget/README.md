# Session Token Budget

> Per-session token cap, cross-turn usage accounting, and estimated cost.

## 🎯 Overview

A session may carry an optional token **cap** (`StateKey.TOKEN_BUDGET_CAP`).
When `TOKEN_BUDGET_USED >= cap`, `TokenBudget`
(`agent/middlewares/token_budget/core.py`) short-circuits the next model call
with a terminal `AIMessage` and never reaches the provider. The turn ends with a
request to summarize; the operator can raise the cap or reset the counters.

The middleware sits beside `IterationBudget` in the main chain and is always on
(not gateable). With no cap it is a pure pass-through. Token counts come from
the provider's `usage_metadata` and are exact; the cost is an estimate.

## ⚙️ Configuration

- `PUT /sessions/budget` sets the cap (`null` clears it, `0` means unlimited).
  A change made while a turn is running is PARKED
  (`TOKEN_BUDGET_CAP_PENDING`) and promoted at the turn boundary, exactly like
  the thinking/model controls.
- `GET /sessions/budget` returns `{cap, used, cost, limited, pending}`.
- `POST /sessions/budget/reset` clears `TOKEN_BUDGET_USED`,
  `TOKEN_BUDGET_COST` and `TOKEN_BUDGET_LIMITED`; the cap is preserved.
- `TOKEN_BUDGET_USED` / `TOKEN_BUDGET_COST` accumulate across turns, are
  dual-written to the mem + DB registers, and are never reset by the per-turn
  iteration budget. `clear_session` removes them with the other session state.
- A `budget_warning` WS frame fires once at
  `MODEL_PRICING["budget_warn_threshold"]` (0.80) and `budget_limited` once when
  the cap is first reached; both are best-effort.

## 🧮 Pricing Lookup

The price table is user-configurable in `.models`:

```jsonc
"MODEL_PRICING_TABLE": "{\"glm-5\": {\"input\": 0.5, \"output\": 1.5}, \"_default\": {\"input\": 0.5, \"output\": 1.5}}"
```

The value is a JSON OBJECT serialized as a JSON string, so the flat `.models`
key/value editor keeps working. A missing table on an existing file is added on
import (the template supplies the default entries).

`TokenBudget._cost_delta` resolves cost in four steps:

1. provider-returned cost in `usage_metadata` / `response_metadata`
   (`cost`, `total_cost`, `estimated_cost`, or `prompt_cost` +
   `completion_cost`) — used verbatim;
2. otherwise `config/features/infra_side/model_pricing.py::resolve_model_pricing`
   looks in the user table (exact model name, then the part after a `/` prefix);
3. then the local litellm cache `src/data/litellm_prices.json` (gitignored;
   refresh with `uv run --no-sync python scripts/sync_litellm_prices.py`);
4. then the table's `_default` (code fallback `input $0.5/M`,
   `output $1.5/M` if the table itself is unusable).

The cap is based on tokens, never on this estimate.

## 🌐 HTTP API

| Method | Path | Body / query |
| :--- | :--- | :--- |
| `GET` | `/sessions/budget` | `?session_id=<sid>` |
| `PUT` | `/sessions/budget` | `{session_id, cap: int \| null}` |
| `POST` | `/sessions/budget/reset` | `{session_id}` |

## 🧪 Tests

`tests/agent/middlewares/test_token_budget.py` (short-circuit, accumulation,
provider cost, warning/limited events), `tests/server/test_token_budget_settings.py`
(idle write, busy park/promote, reset, clearing the latch) and
`tests/config/test_model_pricing.py` (table load, litellm/default priority,
provider-prefixed names) and
`tests/server/trigger/http/test_token_budget_api.py` (HTTP cap → middleware
accumulation → short-circuit → GET report → reset, end to end).
