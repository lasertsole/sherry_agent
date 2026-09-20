# 🧭 Summarization Triggers — Lifecycle T1–T5 & Overflow Routing

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> Part of [Summarization](../README.md): the five lifecycle trigger points (T1–T5) and the four-route overflow decision.

---

## 🧭 Lifecycle: Five Trigger Points (T1–T5)

```
turn starts
│
├─ T1  before_agent preflight  (_t1_preflight :1886 / _at1_preflight :1917)
│      ├─ _reset_turn_state (:1849) resets the 11 per-turn counters
│      ├─ _decide_overflow_route (:632) → None / "fits" → no-op
│      ├─ cooldown > 0 blocks the COMPACT routes; the truncate track
│      │  still runs (it is the cheap recovery mechanism itself)
│      └─ dispatch (trigger="T1") + _t1_state_update (:1810) commits the
│         result to the graph:
│         [RemoveMessage(id=REMOVE_ALL_MESSAGES), *new_messages]
│         (the add_messages reducer never removes by itself — the
│         RemoveMessage sentinel is the only way the compacted prefix
│         actually leaves the state)
│
├─ T2  wrap_model_call, pre-handler (:1960 sync / :2046 async)
│      ├─ read force flag (:1973) BEFORE the skip gate — the skip gate
│      │  (_should_skip_compression :1255) consumes the flag
│      ├─ _tick_cooldown (:832): EVERY call decrements the cooldown
│      ├─ anti-thrash gate (:1984–1987):
│      │    if not forced and (cooldown_active or
│      │               attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN):
│      │      pass through (rebuild system prompt if a compact just
│      │      happened, :1993–2005) → handler → monitor → T3
│      ├─ else: 4-route decision (:2019) → _dispatch_overflow_route;
│      │  elif a legacy trigger clause fires (_check_trigger :576,
│      │  e.g. ("messages", 40)) → ROUTE_COMPACT_ONLY (:2027)
│      └─ all three handler invocation sites (:1979, :2005, :2030) run
│         inside _execute_with_recovery (:1057) — the T4/T5 ring
│
├─ T3  post-response re-check  (_post_response_check :849 / async :922)
│      ├─ skipped when T2 compressed in THIS wrap call (t2_compressed
│      │  flag, :2032–2035) — exactly one compression per model call
│      ├─ extract_reported_input_tokens(response) (:130); None → return
│      ├─ gates: turn-attempt cap, cooldown, usable budget
│      ├─ pressure = max(estimate + system_prompt, reported) — the
│      │  provider-reported input tokens win (compute_pressure)
│      ├─ pressure < usable × 0.80 → return; route "fits" → return
│      └─ dispatch (trigger="T3") and ALWAYS return the ORIGINAL
│         response; the whole body is fail-open (any exception → log,
│         original response preserved)
│
└─ T4/T5  provider-error recovery ring
       (_execute_with_recovery :1057 / _aexecute_with_recovery :1115)
       ├─ handler raises → classify_provider_error
       │    (pub/func/message/llm_error_classifier.py):
       │    payload_too_large → T4, context_overflow → T5
       │    (_TRIGGER_BY_ERROR_CLASS :115, _RETRY_KEY_BY_ERROR_CLASS :119)
       ├─ non-target / unknown class → ORIGINAL exception re-raises
       │  untouched (zero retries, zero state writes, never swallowed)
       ├─ retries < MAX_OVERFLOW_RETRIES (3) → _forced_recovery_request
       │  (:985 / async :1030): the no-LLM tail clip runs FIRST — when it
       │  alone drops the estimate under the usable budget the handler is
       │  retried with stubbed tail results and no compact happens; an
       │  already-stubbed request makes the clip a no-op, so the next
       │  attempt degrades to the compact + budget truncation step, which
       │  bypasses
       │  ALL anti-thrash gates by construction (cooldown, per-turn cap
       │  and _should_skip_compression are never consulted); it does NOT
       │  arm the cooldown or count a turn attempt, but it DOES go
       │  through _record_compression so session stats stay truthful;
       │  the per-class retry counter increments AFTER success (:1021)
       ├─ retries exhausted → ORIGINAL exception re-raises (error-frame
       │  propagation via messages.py → turn_runner.py — never an empty
       │  response)
       └─ forced-compression step itself fails → the ORIGINAL exception
          re-raises (raise exc from compression_exc). _monitor_degradation
          runs once, AFTER the ring returns, on the final success only.
```

The legacy trigger clauses still exist as the T2 fallback (`_check_trigger`, :576): `("messages", N)` fires on history length, `("tokens", N)` fires on `max(local estimate, last AIMessage's reported usage_metadata.total_tokens)` ≥ N. A clause list is an OR.

## 🚦 The Four-Route Overflow Decision

`pub/func/message/overflow_router.py` is a **pure decision layer** — no truncation, no compression, no I/O, no state. The middleware imports three functions:

- `compute_pressure` (:50) = `max(estimated_tokens + system_prompt_tokens, reported_tokens)` — the API-reported number wins when present;
- `find_truncatable_tool_results` (:68) — **only** `ToolMessage`s are eligible (tool results are regenerable); the most recent `TRUNCATABLE_RECENT_SKIP (6)` messages are always excluded so the newest tool/ai pairing stays intact; a candidate must be worth ≥ `MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE (200)` estimated tokens; the result is sorted DESC so executors cut the biggest wins first;
- `decide_route` (:103) — the dispatch contract (stable strings):

| Pressure (`p`) vs `usable` | No truncatable candidates | Candidates exist | Candidate token sum vs overflow (`p − usable`) |
| :------------------------- | :------------------------ | :--------------- | :--------------------------------------------- |
| `p < 0.70 × usable` | `fits` | `fits` | — |
| soft overflow `0.70 × usable ≤ p < 0.80 × usable` | `fits` | `truncate_tool_results_only` | — (compression is **never** triggered by soft overflow alone) |
| hard overflow `p ≥ 0.80 × usable` | `compact_only` | sum ≥ overflow → `truncate_tool_results_only`; sum < overflow → `compact_then_truncate` | overflow = `p − usable` |

All three threshold inputs derive from the **usable budget**, not the raw window:

```
usable_budget  = max(context_window − COMPRESSION_RESERVE_TOKENS(16_000), 0)   # _usable_budget :615
system_est     = estimate_text_tokens(system_prompt)   # _estimate_system_prompt_tokens :770
truncate line  = usable × PREEMPTIVE_TRUNCATE_RATIO (0.70)
compact line   = usable × COMPRESSION_TRIGGER_RATIO (0.80)
truncate budget= usable × TRUNCATE_BUDGET_RATIO (0.60)
```

The single executor `_dispatch_overflow_route` (:760 sync / :800 async) serves T1, T2 **and** T3 — never a second copy:

- `truncate_tool_results_only` → `_run_budget_truncation` (:659) — step 1 truncates oversized tool-call args (returns new messages, see the [truncate track](../internals/README.md#-the-truncate-track-budget-truncation--the-ttl-module)), step 2 truncates tool results in place — then a **recheck**: if the freed tokens were not enough (`new_tokens ≥ usable × 0.80`, estimated on the returned list), escalate to `compact_then_truncate`; otherwise pass through WITHOUT compression;
- `compact_only` / `compact_then_truncate` → `_execute_compact` (:702 / async :731) → `_apply_compression` (exceptions logged, request unchanged) → `_record_compaction_bookkeeping` (:694: arm the cooldown, count the turn attempt) → for `compact_then_truncate`, budget truncation runs on the compacted result as backstop → route logged with old/new tokens and pressure ratio.

**P1-2 fast path — the no-LLM tail clip.** Before any route executes, `_fast_tail_clip` runs `clip_overflow_tail` (`pub/func/message/overflow_clip.py`): the trailing contiguous `ToolMessage` batch is replaced by compact stubs through `ToolMessage.model_copy`, so no message is removed or injected — `id`, `tool_call_id`, `name` and `additional_kwargs` survive, keeping the pairing sanitizer and the persistence watermark satisfied. Budget: `target = max(estimate_messages_tokens(messages, reported_tokens=0) − usable × ratio, 0)`; a `0` target means "take the maximum eligible batch" (bounded by `overflow_clip_max_remove`, floored by `overflow_clip_min_keep`). `ratio` is `COMPRESSION_TRIGGER_RATIO (0.80)` on the route path and `1.0` (below the usable budget) on the T4/T5 forced step. Only a clip that ALONE drops the estimate under the line is accepted: the request is returned with the stubbed list and the route never executes (no budget truncation, no auxiliary-LLM compaction). An insufficient clip is discarded and the existing route runs on the exact original list. On T4/T5 the clip reads `request.messages`; an already-stubbed request makes it a no-op, so the retry budget cannot be burned on identical clips and the next attempt degrades to compression.

**Why dropping tail content is safe:** every tool result was already flushed to MesMemory the moment it returned (`MessagePersistenceMiddleware`) and stays retrievable through the `message_search` tool. P0-2-evicted results keep their `[evicted to: …]` pointer inside the stub (so `read_file` still works), P2-4-sliced `read_file` results keep their slice notice verbatim, and a stubbed message ends the scannable batch — a second clip pass is a no-op and never destroys those markers. Config: `overflow_clip_enabled` / `overflow_clip_max_remove` / `overflow_clip_min_keep`.

Window math (test contracts): window `41 600` → usable `25 600`, lines `17 920` / `20 480`, truncate budget `15 360`. With the test-pinned `MAIN_LLM_MAX_TOKEN = 65536` (the runtime `.env` value must be >= 131072 / 128K) the registered T2 clause sits at `52 428`.
