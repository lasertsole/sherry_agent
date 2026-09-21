# 🗜️ Context Compaction: the Summarization Middleware

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> How the agent keeps long conversations inside the model's context window: five trigger points watch the whole lifecycle (before the turn, before every model call, after every model response, and on provider overflow errors), a pure 4-route router picks the cheapest fix (truncate big tool results and oversized tool-call args first, AI-compact only when forced), and anti-thrash guards make sure compression can never spiral.

Source of truth: `agent/middlewares/summarization/core.py`, `agent/middlewares/summarization/compression.py`, `agent/middlewares/summarization/overflow.py`, `agent/middlewares/summarization/summary_generation.py`, `agent/middlewares/summarization/thrash.py`, `agent/middlewares/summarization/state_aliases.py`, `agent/middlewares/summarization/plan_context.py`, `pub/func/message/overflow_router.py`, `pub/func/message/tool_result_ttl.py`, `pub/func/message/llm_error_classifier.py`, `pub/func/estimate_tokens.py`, `pub/func/message/tool_output_dedup.py`, `pub/func/message/tool_output_prune.py`, `pub/func/message/target_truncation.py`, `pub/func/message/tool_args_truncate.py`, `pub/func/message/turn_utils.py`, `config/features/agent_side/summarization.py`, plus the two registration sites `agent/core.py` and `agent/tools/subagent/spawn/core.py`. Every line number and constant in this document was verified against that code.

## Table of Contents

- [Overview](#-overview)
- [Triggers: Lifecycle & Overflow Routing](triggers/README.md)
  - [🧭 Lifecycle: Five Trigger Points (T1–T5)](triggers/README.md#-lifecycle-five-trigger-points-t1t5)
  - [🚦 The Four-Route Overflow Decision](triggers/README.md#-the-four-route-overflow-decision)
- [Token Estimation (No Tokenizer)](#-token-estimation-no-tokenizer)
- [Compression Internals](internals/README.md)
  - [✂️ The Truncate Track: Budget Truncation & the TTL Module](internals/README.md#-the-truncate-track-budget-truncation--the-ttl-module)
  - [🔁 The Compact Track: Inside `_apply_compression`](internals/README.md#-the-compact-track-inside-_apply_compression)
  - [🖼️ Compression-Time Media Offload (Inline Media → Reference)](internals/README.md#-compression-time-media-offload-inline-media--reference)
  - [📝 LLM Summary: Prompt, Chaining, Fallback](internals/README.md#-llm-summary-prompt-chaining-fallback)
  - [🧱 The Static Fallback (LLM-Free Summary)](internals/README.md#-the-static-fallback-llm-free-summary)
  - [📦 The Output: Summary Message Pair](internals/README.md#-the-output-summary-message-pair)
  - [🛡️ Anti-Thrash Guard Matrix & Degradation Recovery](internals/README.md#-anti-thrash-guard-matrix--degradation-recovery)
  - [🔄 System Prompt Refresh](internals/README.md#-system-prompt-refresh)
  - [📌 Registration Sites](internals/README.md#-registration-sites)
- [Configuration Reference](#-configuration-reference)
- [Testing](#-testing)
- [⚠️ Honesty & Limitations](#%EF%B8%8F-honesty--limitations)

## 🎯 Overview

`Summarization` (`agent/middlewares/summarization/core.py`, class at `core.py:234`) is a **from-scratch** `AgentMiddleware` — it does **not** inherit from LangChain's built-in `SummarizationMiddleware`. It hooks exactly two points of the agent lifecycle:

- `before_agent` / `abefore_agent` (`core.py:399` / `core.py:403`) — **T1 preflight**
- `wrap_model_call` / `awrap_model_call` (`core.py:413` / `core.py:495`) — **T2 dispatch, T3 post-response re-check, T4/T5 error-recovery ring**

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
| Main agent (`agent/core.py:198`) | `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `True` |
| Worker/subagent (`agent/tools/subagent/spawn/core.py:847`) | `("messages", 40)` **or** `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `False` (default) |

Both pass `main_llm_context_window=main_llm_max_tokens` (from `MAIN_LLM_MAX_TOKEN`) and `keep=("messages", 10)`.

## 🪙 Token Estimation (No Tokenizer)

`pub/func/estimate_tokens.py` (230 lines) is deliberately tokenizer-free and deterministic, with a three-tier fallback:

- **T1 — API-reported usage:** `estimate_messages_tokens` returns the last `AIMessage`'s `usage_metadata` (or an explicit `reported_tokens`) verbatim when present — the provider's ground-truth count short-circuits all local estimation;
- **T2 — CJK-aware heuristic:** `estimate_text_tokens` splits the text into CJK characters (`// CHARS_PER_TOKEN_CJK = 2`) and the rest (`// CHARS_PER_TOKEN = 4`), reusing `pub.func.cjk.count_cjk` for detection;
- **T3 — legacy `len // 4`:** not a separate code path — the T2 degenerate case when `count_cjk(text) == 0`, so pure-ASCII estimates keep the legacy numbers exactly.

```python
tokens = (cjk chars // CHARS_PER_TOKEN_CJK)   # CHARS_PER_TOKEN_CJK = 2
       + (other chars // CHARS_PER_TOKEN)     # CHARS_PER_TOKEN = 4
# message-level: str content → text estimate
#              + Σ tool_call name/args chars + tool_call_id chars
# list content → per block: text blocks as text, media blocks at a fixed
#   per-type cost, unknown blocks at the conservative unknown cost
```

A content **list** is counted block by block, never by JSON-serializing the whole list: text blocks are estimated as text, media blocks (`image_url` / `audio_url` / `video_url` / `audio_bytes` / `video_bytes`, or any block carrying a `data:` payload) get a fixed cost from `TOKEN_ESTIMATION` — `tokens_per_image_block = 85`, `tokens_per_audio_block = 256`, `tokens_per_video_block = 1024` — and unknown blocks get `tokens_per_unknown_block = 85`. The removed JSON path counted 5 MB of base64 as ~1.25M tokens and would fire compression on a single image; the fixed costs are conservative stand-ins for media whose real token count is not derivable without a model. `str` content and `None` are unchanged, and a pure-text list estimates like the concatenated text.

`pub/func/message/estimate_msg_tokens.py` is now a backward-compatible re-export of the same helpers. The estimator is fast, stable across runs (same input → same number → reproducible tests), and intentionally conservative-approximate. Nothing in the trigger/budget path depends on a model tokenizer.

## ⚙️ Configuration Reference

All thresholds live in `config/features/agent_side/summarization.py` (SUMMARIZATION TypedDict). Values marked ◆ are consumed by the live code paths; values marked ○ are defined or imported but **not consumed** by any live path (see Honesty & Limitations).

| Constant | Value | Consumed where |
| :------- | :---- | :------------- |
| `COMPRESSION_TRIGGER_RATIO` ◆ | `0.80` | hard-overflow band in `decide_route`; T3 pressure gate; builds both trigger clauses |
| `PREEMPTIVE_TRUNCATE_RATIO` ◆ | `0.70` | soft-overflow band in `decide_route` |
| `COMPRESSION_RESERVE_TOKENS` ◆ | `16_000` | `_usable_budget` (overflow.py:168): window − reserve |
| `TRUNCATE_BUDGET_RATIO` ◆ | `0.60` | truncate-track budget = usable × 0.60 (overflow.py:219) |
| `MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE` ◆ | `200` | candidate floor in `find_truncatable_tool_results` |
| `TRUNCATABLE_RECENT_SKIP` ◆ | `6` | newest messages never truncatable (pairing margin) |
| `MAX_OVERFLOW_RETRIES` ◆ | `3` | T4/T5 forced-recovery cap (shared counter) |
| `OVERFLOW_CLIP_ENABLED` ◆ | `True` | P1-2 no-LLM tail clip master switch |
| `OVERFLOW_CLIP_MAX_REMOVE` ◆ | `10` | max tail messages stubbed per clip pass |
| `OVERFLOW_CLIP_MIN_KEEP` ◆ | `5` | transcript floor: ≤ this many messages → no clip |
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
| `MIN_ARGS_CHARS_TO_TRUNCATE` ◆ | `500` | tool-args truncate: eligibility (JSON-serialized args length) |
| `MAX_TOOL_ARGS_CHARS` ◆ | `2_000` | tool-args truncate: per-args cap |
| `AGGRESSIVE_TRUNCATE_CHARS` ◆ | `1_000` | aggressive backstop cut length (tool results and tool-call args) |
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
| `ACTIVE_PLAN_NOTES_MAX_ITEMS` / `EVICTED_REFS_MAX_ITEMS` ◆ | `20` / `20` | document array caps (plan notes / eviction pointers) |
| `FILE_OPS_LIST_MAX_CHARS` ◆ | `900` | file-ops ratchet list cap |
| `LATEST_USER_REQUEST_MAX_CHARS` ◆ | `800` | recovery-context request cap |
| `CHARS_PER_TOKEN` / `CHARS_PER_TOKEN_CJK` (estimator) | `4` / `2` | deterministic token estimate divisors (non-CJK / CJK); defined in `config/features/agent_side/token_estimation.py` |
| `TOKENS_PER_IMAGE_BLOCK` / `TOKENS_PER_AUDIO_BLOCK` / `TOKENS_PER_VIDEO_BLOCK` / `TOKENS_PER_UNKNOWN_BLOCK` (estimator) | `85` / `256` / `1024` / `85` | fixed per-block costs for a multimodal content list — base64 is never counted as text; defined in `config/features/agent_side/token_estimation.py` |
| `PRUNE_TTL_SECONDS` | `300` | TTL-expiry horizon — consumed only by the TTL trio (test-only today) |
| `TTL_REGISTRY_MAX_ENTRIES` | `512` | TTL first-seen registry bound (test-only today) |
| `SUMMARY_TRIM_TOKENS` ○ | `12_000` | imported by the middleware, never read |
| `AUTO_CONTINUE_PROMPT` ○ | — | imported by the middleware, never read |
| `DEGRADATION_MONITOR_COUNT` ○ | `5` | defined, not imported |
| `FILE_OPS_SECTION_MAX_CHARS` ○ | `2_000` | defined, not imported (only the 900-char list cap is used) |

## 🧪 Testing

| Suite | Cases | Covers |
| :---- | :---- | :----- |
| `tests/pub/func/message/test_overflow_router.py` | 29 | `compute_pressure` / `find_truncatable_tool_results` / `decide_route` bands, candidate rules, stable route strings |
| `tests/pub/func/message/test_tool_result_ttl.py` | 28 | In-place truncation, pairing invariant, non-empty placeholders, registry bound, budget truncation |
| `tests/pub/func/message/test_llm_error_classifier.py` | 56 | 413 status, text hints, 7 overflow patterns, cause-chain depth, read-only guarantees |
| `tests/pub/func/message/test_pub_func_message_tools.py` | 29 | dedup / prune / target-truncate / turn-utils plus tool-args truncation: head+tail format, small-args skip, freed clamp, protected tools, skip-recent, pairing & no-mutation |
| `tests/pub/func/message/test_read_file_slice.py` | 12 | read_file recoverable slice: original path + 1-based offset notice, no line skip, absolute page numbering, byte-identical generic marker, protected / under-budget / fallback paths |
| `tests/config/test_num_contract.py` | 46 | Constants contract (watchdog `CONTRACT_NAMES` covers all documented knobs) |
| `tests/pub/func/message/test_overflow_clip.py` | 21 | P1-2 pure clip: trailing-batch detection, max_remove/min_keep/enabled gates, token target, marker preservation (P0-2 pointer, P2-4 notice), no-op idempotency, pairing invariant |
| `tests/agent/middlewares/test_summarization_overflow_clip.py` | 9 | P1-2 middleware integration: T1/T2 clip without LLM, insufficient-clip degradation, kill switch, T4/T5 clip-then-retry and clip→compression degradation, sync/async parity, sanitizer-unchanged |
| `tests/agent/middlewares/test_compression_comprehensive.py` | 52 | 12 classes: T2 soft-overflow, T2 cooldown, T2 negative/no-op, sync/async parity, T1 preflight, route decision, T3 trigger/three-forms/negative-double, T4/T5 recovery, the full anti-thrash matrix, full-branch parity, chained-summary filtering |
| `tests/agent/middlewares/test_compression_media_offload.py` | 12 | Compression-time inline-media offload: write + pointer, content-hash dedup, placeholder on decode/store failure, preserved-window media untouched, sync/async parity, `evicted_refs` collection |
| `tests/pub/func/test_estimate_tokens_media.py` | 22 | Block-wise multimodal estimation: fixed media costs, 5 MB-base64 regression, unknown blocks hiding media, `str` / `None` / empty-list / pure-text edges |
| `tests/agent/middlewares/test_summary_message_filtering.py` | 6 | Chained-summary filtering: prior pair stripped from the serialized conversation, normal/empty/multi-pair inputs, unmarked legacy human preserved, async `_acreate_summary` mirror |
| `tests/agent/middlewares/test_summary_doc.py` + `test_summary_doc_middleware.py` | 41 | Structured summary: schema coercion, render round-trip + byte stability + section order, code-layer caps + annotations, verbatim latest request, json_mode/json_repair/free-form tiers, prior-doc JSON chaining, legacy-MD transition, evicted-refs collection + carry-forward |
| `tests/agent/middlewares/test_summary_active_plan.py` | 20 | Part 1: plan-active detection (state/todo refs, all-done gate, fail-open), prompt injection on first/update paths, notes inherit/append/cap/clear across chained compressions, verbatim latest request + eviction pointer, multi-message burst |
| `tests/agent/middlewares/test_compression_e2e_static.py` | 18 | 6 end-to-end scenarios + 3 overflow-counter regression tests × 2 registration orders, static-fallback compaction, zero network |
| `tests/agent/middlewares/test_summarization_trigger.py` | 3 | Registration contract (test-pinned window): `MAIN_LLM_MAX_TOKEN = 65 536` → trigger threshold `52 428`; low-token pass-through |
| `tests/agent/middlewares/test_summarization_comprehensive.py` | 140 | Legacy deep suite: cutoff/budget, FIFO caps, fallback, prune/dedup/target-truncate, degradation |
| `tests/agent/middlewares/test_e2e_summarization.py` | 7 | Full-graph hermetic e2e: real `create_agent` chain (capturing stub main, failing stub auxiliary) drives the static-fallback path; zero network, scaled-down window 32 000, skips when MAIN_LLM config is missing |
| `tests/agent/middlewares/message_persistence/` | 31 | Per-boundary flush (+ tool return): each message exactly once, no duplicate across boundaries, restart replay via the persistent watermark, sync + async hooks, missing-session_id skip, HITL denial re-pairing, filter semantics; plus T1/T2/T3 compactions proving zero store writes and the summary pair (both halves) proving zero writes |
| `tests/agent/middlewares/test_compression_nudges.py` | 2 | Compression-time nudge dispatch: memory review + plan extraction fire from the compact path; no-cut compactions dispatch nothing |
| `tests/context_engine/store/test_persisted_message_ids.py` | 3 | Persistent watermark store: idempotent marking, session scoping, cleanup on session deletion, empty-input no-ops |
| `tests/context_engine/store/test_interrupt_marker_approach.py` | 11 | Marker semantics: the summary pair survives later compaction; FACT C fixture (window 26 000 → usable 10 000, truncate line 7 000) |

The full process-isolated suite (`uv run python tests/run_tests_split.py`) passes with **4268 passed / 12 skipped / 0 failed** (GROUP A 3282P/1S + GROUP B 913P/11S + GROUP C 73P).

## ⚠️ Honesty & Limitations

- **`keep=("messages", 10)` is accepted but unused.** The constructor stores it for API compatibility; tail retention is budget-based (`PRESERVE_RATIO` × window clamped to [2 000, 15 000]) plus the router's `TRUNCATABLE_RECENT_SKIP` margin. Changing `keep` has no effect.
- **Doc-verbatim imports.** `json`, `hashlib`, `SUMMARY_TRIM_TOKENS`, and `AUTO_CONTINUE_PROMPT` are imported at the top of `summarization/core.py` but never read. `DEGRADATION_MONITOR_COUNT` and `FILE_OPS_SECTION_MAX_CHARS` are defined in the `SUMMARIZATION` TypedDict in `config/features/agent_side/summarization.py` but consumed by nothing.
- **The TTL registry is not wired into production.** `record_first_seen` / `select_expired` / `truncate_expired` (and `PRUNE_TTL_SECONDS`, `TTL_REGISTRY_MAX_ENTRIES`) are consumed only by tests; the middleware uses exclusively `truncate_to_budget`. A grep of `agent/` finds no production call sites for the TTL trio. The registry is also volatile (in-memory, keyed by `tool_call_id`, lost on restart).
- **Retained-but-inert code.** `_preemptive_check` (overflow.py:148) and `_preemptive_truncate` (compression.py:420) are reference-only: no production call site reaches the two-band preemption they implement.
- **The estimator is a three-tier tokenizer-free heuristic, not a tokenizer.** T1 returns provider-reported usage when available; T2 is the CJK-aware heuristic (CJK characters at `CHARS_PER_TOKEN_CJK = 2`, everything else at `CHARS_PER_TOKEN = 4`); T3 is the legacy `len // 4`, the pure-ASCII degenerate case of T2. It is intentionally deterministic (reproducible tests, stable budgets); `CHARS_PER_TOKEN_CJK = 2` reflects Chinese averaging closer to 1–2 chars/token than 4.
- **Where reported usage wins.** T3 is the only reported-usage-driven trigger (`compute_pressure` takes the max). The T1/T2 route decision is estimate-driven (estimate + system-prompt overhead only); the legacy `_check_trigger` clause fallback uses `max(local estimate, reported)`.
- **T3 never alters the returned response.** A T3 dispatch's durable effects are the in-place truncation of tool results (message objects are shared with the graph state) and the anti-thrash bookkeeping; the compact route's `request.override` at T3 is local and the original response is always returned. The whole T3 body is fail-open.
- **T4/T5 bypass the anti-thrash matrix by design** — that is the point of "forced". After `MAX_OVERFLOW_RETRIES (3)` (shared T4/T5 counter, reset each turn), or if the forced-compression step itself fails, the ORIGINAL provider exception propagates (never swallowed, never replaced by the compression error).
- **Compression is fail-open.** Any exception inside `_apply_compression` is logged and swallowed; the turn proceeds with the uncompressed history.
- **The static fallback is heuristic.** Keyword-based decision/completed classification and path extraction from raw tool args are best-effort; the section skeleton is guaranteed, the content quality is not.
- **The structured tier runs in `json_mode` because the configured endpoint requires it.** `glm-5.3-flash` (openai-compatible) does not emit function calls for `with_structured_output`, so the default function-calling method raises a Pydantic parse error; `_structured_runnable` retries without the `method` kwarg for providers whose `with_structured_output` rejects it, and the `json_repair` + free-form tiers cover the rest. The code-layer caps and the renderer still guarantee the output shape.
- **`_SUMMARY_PREFIX`/`_SUMMARY_SUFFIX`/`<summary>` tags/`lc_source="summarization"` are load-bearing exact strings.** Later-turn chaining (`_extract_previous_summary`), prune stop-condition, and the test suites all match them literally — do not reword them casually.
