# 🗜️ Context Compaction: the Summarization Middleware

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> How the agent keeps long conversations inside the model's context window: five trigger points watch the whole lifecycle (before the turn, before every model call, after every model response, and on provider overflow errors), a pure 4-route router picks the cheapest fix (truncate big tool results first, AI-compact only when forced), and anti-thrash guards make sure compression can never spiral.

Source of truth: `agent/middlewares/summarization.py`, `pub_func/message/overflow_router.py`, `pub_func/message/tool_result_ttl.py`, `pub_func/message/llm_error_classifier.py`, `pub_func/message/estimate_msg_tokens.py`, `pub_func/message/tool_output_dedup.py`, `pub_func/message/tool_output_prune.py`, `pub_func/message/target_truncation.py`, `pub_func/message/turn_utils.py`, `config/num.py`, plus the two registration sites `agent/core.py` and `agent/tools/subagent/spawn/core.py`. Every line number and constant in this document was verified against that code.

## Table of Contents

- [Overview](#-overview)
- [Lifecycle: Five Trigger Points (T1–T5)](#-lifecycle-five-trigger-points-t1t5)
- [The Four-Route Overflow Decision](#-the-four-route-overflow-decision)
- [Token Estimation (No Tokenizer)](#-token-estimation-no-tokenizer)
- [The Truncate Track: Budget Truncation & the TTL Module](#-the-truncate-track-budget-truncation--the-ttl-module)
- [The Compact Track: Inside `_apply_compression`](#-the-compact-track-inside-_apply_compression)
- [LLM Summary: Prompt, Chaining, Fallback](#-llm-summary-prompt-chaining-fallback)
- [The Static Fallback (LLM-Free Summary)](#-the-static-fallback-llm-free-summary)
- [The Output: Summary Message Pair](#-the-output-summary-message-pair)
- [Anti-Thrash Guard Matrix & Degradation Recovery](#-anti-thrash-guard-matrix--degradation-recovery)
- [System Prompt Refresh](#-system-prompt-refresh)
- [Registration Sites](#-registration-sites)
- [Configuration Reference](#-configuration-reference)
- [Testing](#-testing)
- [⚠️ Honesty & Limitations](#%EF%B8%8F-honesty--limitations)

## 🎯 Overview

`Summarization` (`agent/middlewares/summarization.py`, class at line 490) is a **from-scratch** `AgentMiddleware` — it does **not** inherit from LangChain's built-in `SummarizationMiddleware`. It hooks exactly two points of the agent lifecycle:

- `before_agent` / `abefore_agent` (lines 1894 / 1898) — **T1 preflight**
- `wrap_model_call` / `awrap_model_call` (lines 1908 / 1994) — **T2 dispatch, T3 post-response re-check, T4/T5 error-recovery ring**

In the middleware chain it sits **innermost — closest to the LLM**. When compression fires, the history always ends up in the shape:

```
HumanMessage("What did we do so far?")
AIMessage(<summary>, lc_source="summarization")
<recent turns preserved verbatim>
```

Because the replacement is a Human/AI pair, the model never sees two consecutive same-role messages and no pairing repair is needed.

Two registrations exist:

| Site | Trigger | LLM | `need_update_system_prompt` |
| :--- | :------ | :-- | :-------------------------- |
| Main agent (`agent/core.py:152`) | `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `True` |
| Worker/subagent (`agent/tools/subagent/spawn/core.py:755`) | `("messages", 40)` **or** `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `False` (default) |

Both pass `main_llm_context_window=main_llm_max_tokens` (from `MAIN_LLM_MAX_TOKEN`) and `keep=("messages", 10)`.

## 🧭 Lifecycle: Five Trigger Points (T1–T5)

```
turn starts
│
├─ T1  before_agent preflight  (_t1_preflight :1834 / _at1_preflight :1865)
│      ├─ _reset_turn_state (:1797) resets the 10 per-turn counters
│      ├─ _decide_overflow_route (:622) → None / "fits" → no-op
│      ├─ cooldown > 0 blocks the COMPACT routes; the truncate track
│      │  still runs (it is the cheap recovery mechanism itself)
│      └─ dispatch (trigger="T1") + _t1_state_update (:1810) commits the
│         result to the graph:
│         [RemoveMessage(id=REMOVE_ALL_MESSAGES), *new_messages]
│         (the add_messages reducer never removes by itself — the
│         RemoveMessage sentinel is the only way the compacted prefix
│         actually leaves the state)
│
├─ T2  wrap_model_call, pre-handler (:1908 sync / :1994 async)
│      ├─ read force flag (:1921) BEFORE the skip gate — the skip gate
│      │  (_should_skip_compression :1234) consumes the flag
│      ├─ _tick_cooldown (:811): EVERY call decrements the cooldown
│      ├─ anti-thrash gate (:1931–1934):
│      │    if not forced and (cooldown_active or
│      │               attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN):
│      │      pass through (rebuild system prompt if a compact just
│      │      happened, :1938–1952) → handler → monitor → T3
│      ├─ else: 4-route decision (:1967) → _dispatch_overflow_route;
│      │  elif a legacy trigger clause fires (_check_trigger :566,
│      │  e.g. ("messages", 40)) → ROUTE_COMPACT_ONLY (:1972)
│      └─ all three handler invocation sites (:1927, :1953, :1978) run
│         inside _execute_with_recovery (:1036) — the T4/T5 ring
│
├─ T3  post-response re-check  (_post_response_check :828 / async :901)
│      ├─ skipped when T2 compressed in THIS wrap call (t2_compressed
│      │  flag, :1980–1983) — exactly one compression per model call
│      ├─ extract_reported_input_tokens(response) (:127); None → return
│      ├─ gates: turn-attempt cap, cooldown, usable budget
│      ├─ pressure = max(estimate + system_prompt, reported) — the
│      │  provider-reported input tokens win (compute_pressure)
│      ├─ pressure < usable × 0.80 → return; route "fits" → return
│      └─ dispatch (trigger="T3") and ALWAYS return the ORIGINAL
│         response; the whole body is fail-open (any exception → log,
│         original response preserved)
│
└─ T4/T5  provider-error recovery ring
       (_execute_with_recovery :1036 / _aexecute_with_recovery :1094)
       ├─ handler raises → classify_provider_error
       │    (pub_func/message/llm_error_classifier.py):
       │    payload_too_large → T4, context_overflow → T5
       │    (_TRIGGER_BY_ERROR_CLASS :112, _RETRY_KEY_BY_ERROR_CLASS :116)
       ├─ non-target / unknown class → ORIGINAL exception re-raises
       │  untouched (zero retries, zero state writes, never swallowed)
       ├─ retries < MAX_OVERFLOW_RETRIES (3) → _forced_recovery_request
       │  (:964 / async :1009): compact + budget truncation that bypasses
       │  ALL anti-thrash gates by construction (cooldown, per-turn cap
       │  and _should_skip_compression are never consulted); it does NOT
       │  arm the cooldown or count a turn attempt, but it DOES go
       │  through _record_compression so session stats stay truthful;
       │  the per-class retry counter increments AFTER success (:1000)
       ├─ retries exhausted → ORIGINAL exception re-raises (error-frame
       │  propagation via messages.py → turn_runner.py — never an empty
       │  response)
       └─ forced-compression step itself fails → the ORIGINAL exception
          re-raises (raise exc from compression_exc). _monitor_degradation
          runs once, AFTER the ring returns, on the final success only.
```

The legacy trigger clauses still exist as the T2 fallback (`_check_trigger`, :566): `("messages", N)` fires on history length, `("tokens", N)` fires on `max(local estimate, last AIMessage's reported usage_metadata.total_tokens)` ≥ N. A clause list is an OR.

## 🚦 The Four-Route Overflow Decision

`pub_func/message/overflow_router.py` is a **pure decision layer** — no truncation, no compression, no I/O, no state. The middleware imports three functions:

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
usable_budget  = max(context_window − COMPRESSION_RESERVE_TOKENS(16_000), 0)   # _usable_budget :605
system_est     = len(state_register_mem["system_prompt"]) // 4                  # :616–620
truncate line  = usable × PREEMPTIVE_TRUNCATE_RATIO (0.70)
compact line   = usable × COMPRESSION_TRIGGER_RATIO (0.80)
truncate budget= usable × TRUNCATE_BUDGET_RATIO (0.60)
```

The single executor `_dispatch_overflow_route` (:739 sync / :779 async) serves T1, T2 **and** T3 — never a second copy:

- `truncate_tool_results_only` → `_run_budget_truncation` (:649) in place, then a **recheck**: if the freed tokens were not enough (`new_tokens ≥ usable × 0.80`), escalate to `compact_then_truncate`; otherwise pass through WITHOUT compression;
- `compact_only` / `compact_then_truncate` → `_execute_compact` (:681 / async :710) → `_apply_compression` (exceptions logged, request unchanged) → `_record_compaction_bookkeeping` (:673: arm the cooldown, count the turn attempt) → for `compact_then_truncate`, budget truncation runs on the compacted result as backstop → route logged with old/new tokens and pressure ratio.

Window math (test contracts): window `41 600` → usable `25 600`, lines `17 920` / `20 480`, truncate budget `15 360`. With `MAIN_LLM_MAX_TOKEN = 65536` the registered T2 clause sits at `52 428`.

## 🪙 Token Estimation (No Tokenizer)

`pub_func/message/estimate_msg_tokens.py` (29 lines) is deliberately tokenizer-free and deterministic:

```python
tokens = (content chars            # str content, or len(json.dumps(content))
        + Σ tool_call name/args chars
        + tool_call_id chars) // CHARS_PER_TOKEN   # CHARS_PER_TOKEN = 4
```

It is fast, stable across runs (same input → same number → reproducible tests), and intentionally conservative-approximate. Nothing in the trigger/budget path depends on a model tokenizer.

## ✂️ The Truncate Track: Budget Truncation & the TTL Module

`pub_func/message/tool_result_ttl.py` provides the in-place truncation used by the truncate track. Design invariants (load-bearing):

- **In place only** — the module never removes, reorders or pops messages; it only mutates `msg.content` (or a content-list block) and returns indices. This preserves the tool-call/`ToolMessage` pairing that the provider API and `ToolCallNormalize` depend on.
- **Non-empty placeholders** — a truncated result always keeps non-empty content: `ToolCallNormalize.before_model` sanitizes the transcript by **dropping empty `ToolMessage`s**, so an empty placeholder would silently break the pairing.
- **30% head / 30% tail keep** (`CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO`) with an omission marker.

What the middleware actually consumes: **only `truncate_to_budget`**, driven by the router's candidate list — `_run_budget_truncation` (:649) truncates candidates until the budget (`usable × TRUNCATE_BUDGET_RATIO`) is met.

The TTL registry itself (`record_first_seen` / `select_expired` / `truncate_expired`, `PRUNE_TTL_SECONDS = 300`, `TTL_REGISTRY_MAX_ENTRIES = 512`, keyed by `tool_call_id`, volatile across restarts) is exercised **only by the test suite** today — the middleware has no age-based expiry wired in (see Honesty & Limitations).

## 🔁 The Compact Track: Inside `_apply_compression`

`_apply_compression` (:1636; async twin :1708) runs, in order:

1. **Capture recovery context** (`_capture_recovery_context`, :1525): the last user request (≤ 800 chars) and the file-operations ratchet — paths extracted from `read`/`write`-family tool calls (:405), merged with the previous round's set (reads are remembered, modified files are never downgraded to read-only).
2. **Non-LLM strategies** (`_run_non_llm_strategies`, :1472): `dedup → prune → target truncate` (details below). These are free — no model call.
3. **LLM-or-not decision**:

   ```
   if tokens_after_non_llm > budget × 2  OR  skip_llm  OR  nothing was reduced:
       summarize [0:cutoff] and rebuild   → strategy "llm_summary" / "fallback"
   else:
       keep as-is                          → strategy "non_llm_sufficient"
   ```

   Non-LLM shrinking is given the first chance; the auxiliary LLM is only spent when the history is still more than twice the preserve budget (or LLM summarization was disabled by the governor, or non-LLM strategies reduced nothing).
4. **Aggressive backstop** (`_aggressive_truncate`, :1508): if the result is *still* too big, every `ToolMessage` > `AGGRESSIVE_TRUNCATE_CHARS (1 000)` chars is hard-cut with a marker.
5. **Summary self-truncation** (`_truncate_summary_messages`, :1578): any existing summary message (`lc_source == "summarization"`) longer than `SUMMARY_TOTAL_MAX_CHARS (16 000)` chars is re-truncated head 30% / tail 30% (`_truncate_content`, :1570).
6. **Recovery injection** (`_inject_recovery_context`, :1543): the captured file-ops ratchet is rewritten into the summary's `## Relevant Files` section, so the checkpoint always carries an up-to-date read/modified file map.
7. **Bookkeeping** (`_record_compression`, :1256) and finally `request.override(messages=..., system_message=...)`.

**Cutoff selection** (`_determine_cutoff`, :1289): split the history into turns, walk **from the newest backwards** accumulating against the preserve budget `clamp(window × 0.25, 2 000, 15 000)` (`_calculate_preserve_budget`, :555); a turn that does not fully fit is split mid-turn. `_adjust_for_orphan_pairs` (:1319) then walks the cutoff backwards until no `ToolMessage` is separated from its `AIMessage` tool-call. Unless the last-turn ratio gate fires (last user turn ≥ `LAST_TURN_RATIO_THRESHOLD (0.5)` of tokens — `_check_last_turn_ratio`, called at wrap entry :1916/:2002), the cutoff never crosses the last `HumanMessage`.

Every failure mode is fail-open: if `_apply_compression` raises, the exception is logged and the original request proceeds unchanged — a broken compaction never breaks the turn.

## 📝 LLM Summary: Prompt, Chaining, Fallback

`_create_summary` / `_acreate_summary` (:1389 / :1414):

1. **Serialize** (`_serialize_for_summary`, :255): each message becomes a tagged line — `[User]:` (≤ 2 000 chars), `[Assistant]:` (≤ 2 000 chars), `[Assistant tool call]: name(args ≤ 500 chars)`, `[Tool result|Tool error] (id):` (> 2 000 chars → keep 1 800 + omission marker).
2. **Chain the prior checkpoint** (`_extract_previous_summary`, :1355): the newest `AIMessage` with `additional_kwargs["lc_source"] == "summarization"` is found and its `<summary>…</summary>` body extracted. If present, the prompt becomes `conversation + prior-summary + _SUMMARY_PROMPT_UPDATE` (:242) instead of `_SUMMARY_PROMPT_FIRST` (:234) — carry objectives/constraints/decisions forward, newest wins conflicts, FIFO limits.
3. **Invoke** the auxiliary model with `config={"metadata": {"lc_source": "summarization"}}` so downstream tooling can identify summary calls.
4. **Guard rails:** a response that is empty or trivially short falls back to the deterministic summary; any exception does the same. The LLM never gets the last word on failure.

The prompt template (`_SUMMARY_TEMPLATE`, :187) fixes the Markdown skeleton — *Latest Unresolved User Request / Goal / Constraints & Preferences / Progress (Completed ≤ 5 · In Progress · Blocked) / Key Decisions ≤ 5 / Next Steps / Critical Context ≤ 3 / Relevant Files* — with "keep every section even when empty" and a secrecy rule ("NEVER include API keys, tokens, passwords, secrets"). `_enforce_fifo_limits` (:371) re-imposes the item caps deterministically on the returned text, appending `"(N earlier items omitted for brevity)"`.

## 🧱 The Static Fallback (LLM-Free Summary)

`_build_static_fallback_summary` (:286) produces the same section skeleton with zero model calls:

- last user request → *Latest Unresolved User Request*; first request → *Goal*;
- AI text containing decision keywords (`decided`, `choosing`, `because`, `therefore`) → *Key Decisions*, else *Completed*;
- every tool call → *Completed*; path-like tokens (contains `/` or `\`, or ends in `.py`/`.md`/`.js`/`.ts`/`.json`) → *Relevant Files* (≤ 10, `http` links excluded);
- error `ToolMessage`s → *Blocked* and *Critical Context*.

It is used verbatim when `skip_llm` is active, and as the safety net for short/failed LLM summaries.

## 📦 The Output: Summary Message Pair

`_build_new_messages` (:1443) wraps the summary text and emits exactly two messages:

```
[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted …
Respond ONLY to the latest user message that appears AFTER this summary.

<summary>
…summary Markdown…
</summary>

--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---
```

- **HumanMessage** `"What did we do so far?"` — a neutral question that keeps role alternation intact.
- **AIMessage** with `additional_kwargs={"lc_source": "summarization"}` — the marker that later turns use to (a) find and chain the prior checkpoint, (b) make pruning stop at the checkpoint, and (c) let tests assert the summary is swallable from the model view once superseded.
- Total content is capped at `SUMMARY_TOTAL_MAX_CHARS (16 000)` with a head/tail 30/30 keep.

## 🛡️ Anti-Thrash Guard Matrix & Degradation Recovery

State lives in session-scoped `state_register_mem` under **fourteen** `summarization_*` keys (:89–104). `_reset_turn_state` (:1797) resets **ten** of them at every turn start; `summarization_last_user_question`, `summarization_cooldown_rounds` and the two T4/T5 retry counters are deliberately **not** reset per turn.

| Guard | Key | Threshold | Effect |
| :---- | :-- | :-------- | :----- |
| Turn cooldown | `summarization_cooldown_rounds` | `COMPACTION_COOLDOWN_ROUNDS = 3` | Armed after every actual compact (:673); ticked down by **every** model call (:811); blocks T1 compact routes, T2 proactive and T3 — never the T4/T5 forced ring |
| Per-turn compactions | `summarization_turn_attempts` | `MAX_COMPRESS_ATTEMPTS_PER_TURN = 3` | Incremented by :673; suppresses T2 proactive + T3 (forced ring exempt) |
| Per-class overflow retries | `summarization_overflow_retries_t4` / `_t5` | `MAX_OVERFLOW_RETRIES = 3` | Incremented after each successful forced step; exhausted → original provider error propagates |
| Session compressions | `summarization_compression_count` | `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5` | `_should_skip_compression` (:1234) returns True — proactive compression stops entirely |
| Consecutive ineffective | `summarization_compression_ineffective` | `INEFFECTIVE_THRESHOLD = 2` | Sets `skip_llm` — non-LLM strategies only |
| Effectiveness | (`_record_compression`, :1256) | message count reduced **or** token reduction ≥ `MIN_EFFECTIVENESS_PCT (0.05)` | Successful non-LLM strategies (`dedup`/`prune`/`truncate`/`fallback`/`aggressive`) clear `skip_llm` again |
| Degradation recovery budget | `summarization_recovery_attempts` | `MAX_RECOVERY_ATTEMPTS = 2` | Caps forced recoveries from the degradation monitor |

**Degradation monitor** (`_monitor_degradation`, :1609): only consulted when a compaction actually happened this call (`_compaction_just_happened` flag). If the model's reply has no text, a counter increments; at `DEGRADATION_NO_TEXT_THRESHOLD (3)` consecutive empty replies — and while `summarization_recovery_attempts < 2` — it sets `force_recovery`, clears the ineffective streak and the session compression count. Any non-empty reply resets the counter. This catches the pathological "compact → model confused → empty output → compact again" loop. Note the interplay: the forced flag is read at wrap entry (:1921) **before** `_should_skip_compression`, and the skip gate consumes it by resetting the counters and proceeding (:1235–1240) — recovery compression runs exactly once.

## 🔄 System Prompt Refresh

Main agent only (`need_update_system_prompt=True`): after a compression the middleware rebuilds the system prompt and writes it to the `system_prompt` state key, so the next model call sees persona files / long-term memory as they are now. Two delivery paths: `request.override(system_message=SystemMessage(...))` directly after compaction, and — when a T1 compact already happened but the anti-thrash gate blocks a second one — the rebuilt prompt is still injected in the gate path (:1938–1952), because chains without `ContextEngineHook` rely on this middleware delivering it.

## 📌 Registration Sites

```python
# agent/core.py:152 — main agent (Summarization is the LAST middleware:
# innermost wrap layer, closest to the LLM)
Summarization(
    need_update_system_prompt=True,
    model=auxiliary_llm,
    main_llm_context_window=main_llm_max_tokens,
    trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
    keep=("messages", 10),
)

# agent/tools/subagent/spawn/core.py:755 — worker agent (first middleware)
Summarization(
    model=auxiliary_llm,
    main_llm_context_window=main_llm_max_tokens,
    trigger=[
        ("messages", 40),
        ("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO)),
    ],
    keep=("messages", 10),
)
```

## ⚙️ Configuration Reference

All thresholds live in `config/num.py`. Values marked ◆ are consumed by the live code paths; values marked ○ are defined or imported but **not consumed** by any live path (see Honesty & Limitations).

| Constant | Value | Consumed where |
| :------- | :---- | :------------- |
| `COMPRESSION_TRIGGER_RATIO` ◆ | `0.80` | hard-overflow band in `decide_route`; T3 pressure gate; builds both trigger clauses |
| `PREEMPTIVE_TRUNCATE_RATIO` ◆ | `0.70` | soft-overflow band in `decide_route` (the old `_preemptive_check` two-band gate is retired) |
| `COMPRESSION_RESERVE_TOKENS` ◆ | `16_000` | `_usable_budget` (:605): window − reserve |
| `TRUNCATE_BUDGET_RATIO` ◆ | `0.60` | truncate-track budget = usable × 0.60 (:660) |
| `MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE` ◆ | `200` | candidate floor in `find_truncatable_tool_results` |
| `TRUNCATABLE_RECENT_SKIP` ◆ | `6` | newest messages never truncatable (pairing margin) |
| `MAX_OVERFLOW_RETRIES` ◆ | `3` | T4/T5 forced-recovery cap per error class |
| `MAX_COMPRESS_ATTEMPTS_PER_TURN` ◆ | `3` | per-turn proactive compaction cap |
| `COMPACTION_COOLDOWN_ROUNDS` ◆ | `3` | cooldown armed after every actual compact |
| `MIN_PRESERVE_TOKENS` ◆ | `2_000` | preserve-budget floor; budget without a window |
| `MAX_PRESERVE_TOKENS` ◆ | `15_000` | preserve-budget ceiling |
| `PRESERVE_RATIO` ◆ | `0.25` | preserve budget = 25% of window |
| `PRUNE_PROTECT_TOKENS` ◆ | `40_000` | prune: newest tool-output tokens kept |
| `PRUNE_MIN_REDUCTION_TOKENS` ◆ | `5_000` | prune: minimum payoff to apply |
| `TARGET_TRUNCATE_RATIO` ◆ | `0.5` | target-truncate: shrink toward 50% of current tokens |
| `MIN_OUTPUT_CHARS_TO_TRUNCATE` ◆ | `500` | target-truncate: eligibility |
| `MAX_TOOL_OUTPUT_CHARS` ◆ | `2_000` | target-truncate: per-output cap |
| `AGGRESSIVE_TRUNCATE_CHARS` ◆ | `1_000` | aggressive backstop cut length |
| `SUMMARY_TOTAL_MAX_CHARS` ◆ | `16_000` | summary message char cap |
| `CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO` ◆ | `0.3` / `0.3` | all head/tail keeps (summaries and TTL truncation) |
| `DEGRADATION_NO_TEXT_THRESHOLD` ◆ | `3` | empty replies before forced recovery |
| `MAX_RECOVERY_ATTEMPTS` ◆ | `2` | degradation-recovery budget |
| `MAX_TOTAL_COMPRESSION_ATTEMPTS` ◆ | `5` | governor: session attempt cap |
| `INEFFECTIVE_THRESHOLD` ◆ | `2` | governor: consecutive ineffective → skip LLM |
| `MIN_EFFECTIVENESS_PCT` ◆ | `0.05` | governor: token-reduction effectiveness |
| `PROTECTED_TOOLS` ◆ | `{"memory", "skill_view", "skill_list"}` | exempt from every shrink strategy |
| `LAST_TURN_RATIO_THRESHOLD` ◆ | `0.5` | last-turn compression gate |
| `COMPLETED_MAX_ITEMS` / `KEY_DECISIONS_MAX_ITEMS` / `CRITICAL_CONTEXT_MAX_ITEMS` ◆ | `5` / `5` / `3` | FIFO section caps |
| `FILE_OPS_LIST_MAX_CHARS` ◆ | `900` | file-ops ratchet list cap |
| `LATEST_USER_REQUEST_MAX_CHARS` ◆ | `800` | recovery-context request cap |
| `CHARS_PER_TOKEN` (estimator) | `4` | deterministic token estimate divisor |
| `PRUNE_TTL_SECONDS` | `300` | TTL-expiry horizon — consumed only by the TTL trio (test-only today) |
| `TTL_REGISTRY_MAX_ENTRIES` | `512` | TTL first-seen registry bound (test-only today) |
| `SUMMARY_TRIM_TOKENS` ○ | `12_000` | imported by the middleware, never read |
| `AUTO_CONTINUE_PROMPT` ○ | — | imported by the middleware, never read |
| `DEGRADATION_MONITOR_COUNT` ○ | `5` | defined, not imported |
| `FILE_OPS_SECTION_MAX_CHARS` ○ | `2_000` | defined, not imported (only the 900-char list cap is used) |

## 🧪 Testing

| Suite | Cases | Covers |
| :---- | :---- | :----- |
| `tests/unit/test_overflow_router.py` | 29 | `compute_pressure` / `find_truncatable_tool_results` / `decide_route` bands, candidate rules, stable route strings |
| `tests/unit/test_tool_result_ttl.py` | 28 | In-place truncation, pairing invariant, non-empty placeholders, registry bound, budget truncation |
| `tests/unit/test_llm_error_classifier.py` | 20 | 413 status, text hints, 7 overflow patterns, cause-chain depth, read-only guarantees |
| `tests/unit/test_config_num.py` | 43 | Constants contract (watchdog `CONTRACT_NAMES` covers all documented knobs) |
| `tests/module/test_compression_comprehensive.py` | 48 | 12 classes: T2 soft-overflow, T2 cooldown, T2 negative/no-op, sync/async parity, T1 preflight, route decision, T3 trigger/three-forms/negative-double, T4/T5 recovery, the full anti-thrash matrix, full-branch parity |
| `tests/module/test_compression_e2e_static.py` | 12 | 6 end-to-end scenarios × 2 registration orders, static-fallback compaction, zero network |
| `tests/module/test_summarization_trigger.py` | 3 | Production registration contract: `MAIN_LLM_MAX_TOKEN = 65 536` → trigger threshold `52 428`; low-token pass-through |
| `tests/module/test_summarization_comprehensive.py` | 140 | Legacy deep suite: cutoff/budget, FIFO caps, fallback, prune/dedup/target-truncate, degradation |
| `tests/module/test_e2e_summarization.py` | 7 | Full-graph hermetic e2e: real `create_agent` chain (capturing stub main, failing stub auxiliary) drives the static-fallback path; zero network, scaled-down window 32 000, skips when MAIN_LLM config is missing |
| `tests/integration/test_interrupt_marker_approach.py` | 11 | Marker semantics: the summary pair survives later compaction; FACT C fixture (window 26 000 → usable 10 000, truncate line 7 000) |

The full process-isolated suite (`uv run python tests/run_tests_split.py`) passes with **2219 passed / 0 failed** (GROUP A 1469P/2S + GROUP B 750P/5D).

## ⚠️ Honesty & Limitations

- **`keep=("messages", 10)` is accepted but unused.** The constructor stores it for API compatibility; tail retention is budget-based (`PRESERVE_RATIO` × window clamped to [2 000, 15 000]) plus the router's `TRUNCATABLE_RECENT_SKIP` margin. Changing `keep` has no effect.
- **Doc-verbatim imports.** `json`, `hashlib`, `SUMMARY_TRIM_TOKENS`, and `AUTO_CONTINUE_PROMPT` are imported at the top of `summarization.py` but never read. `DEGRADATION_MONITOR_COUNT` and `FILE_OPS_SECTION_MAX_CHARS` are defined in `config/num.py` but consumed by nothing.
- **The TTL registry is not wired into production.** `record_first_seen` / `select_expired` / `truncate_expired` (and `PRUNE_TTL_SECONDS`, `TTL_REGISTRY_MAX_ENTRIES`) are consumed only by tests; the middleware uses exclusively `truncate_to_budget`. A grep of `agent/` finds no production call sites for the TTL trio. The registry is also volatile (in-memory, keyed by `tool_call_id`, lost on restart).
- **Retained-but-inert code.** `_preemptive_check` (:579) and `_preemptive_truncate` (:1138) have no call sites anymore — the two-band preemption they implemented was replaced by the 4-route decision. They are kept for reference.
- **The estimator is `chars // 4`, not a tokenizer.** It is intentionally deterministic (reproducible tests, stable budgets) and calibrated for mixed English/code; CJK-heavy content will be under-counted (Chinese averages closer to 1–2 chars/token than 4).
- **Where reported usage wins.** T3 is the only reported-usage-driven trigger (`compute_pressure` takes the max). The T1/T2 route decision is estimate-driven (estimate + system-prompt overhead only); the legacy `_check_trigger` clause fallback uses `max(local estimate, reported)`.
- **T3 never alters the returned response.** A T3 dispatch's durable effects are the in-place truncation of tool results (message objects are shared with the graph state) and the anti-thrash bookkeeping; the compact route's `request.override` at T3 is local and the original response is always returned. The whole T3 body is fail-open.
- **T4/T5 bypass the anti-thrash matrix by design** — that is the point of "forced". After `MAX_OVERFLOW_RETRIES (3)` per class, or if the forced-compression step itself fails, the ORIGINAL provider exception propagates (never swallowed, never replaced by the compression error).
- **Compression is fail-open.** Any exception inside `_apply_compression` is logged and swallowed; the turn proceeds with the uncompressed history.
- **The static fallback is heuristic.** Keyword-based decision/completed classification and path extraction from raw tool args are best-effort; the section skeleton is guaranteed, the content quality is not.
- **`_SUMMARY_PREFIX`/`_SUMMARY_SUFFIX`/`<summary>` tags/`lc_source="summarization"` are load-bearing exact strings.** Later-turn chaining (`_extract_previous_summary`), prune stop-condition, and the test suites all match them literally — do not reword them casually.
