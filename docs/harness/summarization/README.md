# 🗜️ Context Compaction: the Summarization Middleware

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> How the agent keeps long conversations inside the model's context window: a deterministic token estimator raises the alarm, non-LLM strategies shrink tool noise for free, and only when that is not enough does an auxiliary LLM rewrite the old turns into a structured checkpoint — with anti-thrashing guards so compression can never spiral.

Source of truth: `agent/middlewares/summarization.py`, `pub_func/message/estimate_msg_tokens.py`, `pub_func/message/tool_output_dedup.py`, `pub_func/message/tool_output_prune.py`, `pub_func/message/target_truncation.py`, `pub_func/message/turn_utils.py`, `config/num.py`, plus the two registration sites `agent/core.py` and `agent/tools/subagent/spawn/core.py`. Every line number and constant in this document was verified against that code.

## Table of Contents

- [Overview](#-overview)
- [Where It Runs: the Per-Model-Call Flow](#-where-it-runs-the-per-model-call-flow)
- [Triggering: Three Gates](#-triggering-three-gates)
- [Token Estimation (No Tokenizer)](#-token-estimation-no-tokenizer)
- [The Preserve Budget & Cutoff](#-the-preserve-budget--cutoff)
- [Compression Pipeline Inside `_apply_compression`](#-compression-pipeline-inside-_apply_compression)
- [LLM Summary: Prompt, Chaining, Fallback](#-llm-summary-prompt-chaining-fallback)
- [The Static Fallback (LLM-Free Summary)](#-the-static-fallback-llm-free-summary)
- [Non-LLM Strategies](#-non-llm-strategies)
- [The Output: Summary Message Pair](#-the-output-summary-message-pair)
- [Anti-Thrashing & Degradation Recovery](#-anti-thrashing--degradation-recovery)
- [System Prompt Refresh](#-system-prompt-refresh)
- [Registration Sites](#-registration-sites)
- [Configuration Reference](#-configuration-reference)
- [Testing](#-testing)
- [⚠️ Honesty & Limitations](#%EF%B8%8F-honesty--limitations)

## 🎯 Overview

`Summarization` (`agent/middlewares/summarization.py`, class at line 402) is a **from-scratch** `AgentMiddleware` — it does **not** inherit from LangChain's built-in `SummarizationMiddleware`. All compaction logic is self-contained: trigger checking, cutoff determination, summary generation, a multi-strategy shrink pipeline, and degradation monitoring.

Its job: when the conversation grows past a threshold, replace the old prefix of the message history with a compact checkpoint while preserving the most recent context verbatim. After a compression the history always has the shape:

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

## 🧭 Where It Runs: the Per-Model-Call Flow

The middleware hooks `before_agent`/`abefore_agent` (counter reset) and `wrap_model_call`/`awrap_model_call` (lines 1188–1262). In the middleware chain it sits **innermost — closest to the LLM** — so its message rewrite is the last thing that happens before the model call.

```
wrap_model_call(request, handler)
│
├─ 1. _check_last_turn_ratio      last turn ≥ 50% of tokens? → flag it
├─ 2. _should_skip_compression    max attempts reached / LLM marked ineffective?
│        └─ yes → call handler directly, monitor response, return
├─ 3. _preemptive_check           pressure = est_tokens / context_window
│        ├─ ≥ 0.80            → "compact"
│        ├─ ≥ 0.70            → "truncate_only"
│        └─ else              → None
├─ 4. if truncate_only|compact:
│        _preemptive_truncate    shrink oversized ToolMessages (> 2000 chars),
│                                no LLM call, override request messages
├─ 5. need_compress = (action == "compact") OR configured trigger fires
├─ 6. if need_compress: _apply_compression(...)   exceptions logged, never fatal
├─ 7. response = handler(request)
└─ 8. _monitor_degradation(response)   count empty responses after compaction
```

The full pipeline is mirrored in `_aapply_compression` for the async path (lines 1087–1153); the two are semantically identical.

## 🚦 Triggering: Three Gates

**Gate 1 — configured trigger** (`_check_trigger`, line 478). Each clause is `("messages", N)` (history length ≥ N) or `("tokens", N)` (effective tokens ≥ N). A list of clauses is an OR.

**Gate 2 — preemptive pressure** (`_preemptive_check`, line 491). Requires `main_llm_context_window` to be set; computes `pressure = effective_tokens / context_window` and returns:

- `"compact"` at `pressure ≥ COMPRESSION_TRIGGER_RATIO (0.80)` — full compression this call;
- `"truncate_only"` at `pressure ≥ PREEMPTIVE_TRUNCATE_RATIO (0.70)` — LLM-free tool-output shrinking only.

**Gate 3 — last-turn ratio** (`_check_last_turn_ratio`, line 581). If the last user turn alone accounts for ≥ `LAST_TURN_RATIO_THRESHOLD (0.5)` of all tokens, `_compress_last_turn` is set: the cutoff logic will be allowed to summarize **into** the final turn instead of protecting it. The last user question is stashed in session state for recovery context.

"Effective tokens" = `max(local estimate, last AIMessage's reported usage_metadata.total_tokens)` — the API-reported number wins when present because it is ground truth (lines 455–461, 478–511).

## 🪙 Token Estimation (No Tokenizer)

`pub_func/message/estimate_msg_tokens.py` is deliberately tokenizer-free and deterministic:

```python
tokens = (content chars            # str content, or len(json.dumps(content))
        + Σ tool_call name/args chars
        + tool_call_id chars) // CHARS_PER_TOKEN   # CHARS_PER_TOKEN = 4
```

It is fast, stable across runs (same input → same number → reproducible tests), and intentionally conservative-approximate. Nothing in the trigger/budget path depends on a model tokenizer.

## 💰 The Preserve Budget & Cutoff

**Budget** (`_calculate_preserve_budget`, line 467):

```
budget = clamp(context_window × PRESERVE_RATIO(0.25), MIN_PRESERVE_TOKENS(2000), MAX_PRESERVE_TOKENS(15000))
without a context window → MIN_PRESERVE_TOKENS (2000)
```

**Cutoff** (`_determine_cutoff`, line 668) selects which tail of the history survives verbatim:

1. Split the history into turns (`split_into_turns`), then walk **from the newest backwards**, accumulating turn sizes until the budget is filled.
2. A turn that does not fully fit can be split mid-turn (`split_turn`) to use the remaining budget exactly.
3. `_adjust_for_orphan_pairs` (line 698) then walks the cutoff **backwards** until no `ToolMessage` is separated from its `AIMessage` tool-call — a tool result whose call got summarized away would be an API error.
4. Unless `_compress_last_turn` is set, the cutoff is clamped to never cross the last `HumanMessage` — the active question is always preserved verbatim.

⚠️ **The noop trap:** if the entire history fits inside the budget, the backward walk never moves the cutoff and it stays `0` — no summarization happens this round (`cutoff == 0 → "noop"`, lines 1045–1047/1117–1119). With the default floor `MIN_PRESERVE_TOKENS = 2000`, histories smaller than ~2000 estimated tokens are never LLM-summarized. Integration tests inject a small `main_llm_context_window` (e.g. 8 000) to exercise the summarizing path deterministically.

## 🔁 Compression Pipeline Inside `_apply_compression`

`_apply_compression` (line 1015; async twin 1087) runs, in order:

1. **Capture recovery context** (`_capture_recovery_context`, line 904): the last user request (≤ 800 chars) and the file-operations ratchet — paths extracted from `read`/`write`-family tool calls, merged with the previous round's set (reads are remembered, modified files are never downgraded to read-only).
2. **Non-LLM strategies** (`_run_non_llm_strategies`, line 851): `dedup → prune → target truncate` (details below). These are free — no model call.
3. **LLM-or-not decision** (line 1030):

   ```
   if tokens_after_non_llm > budget × 2  OR  skip_llm  OR  nothing was reduced:
       summarize [0:cutoff] and rebuild   → strategy "llm_summary" / "fallback"
   else:
       keep as-is                          → strategy "non_llm_sufficient"
   ```

   Non-LLM shrinking is given the first chance; the auxiliary LLM is only spent when the history is still more than twice the preserve budget (or LLM summarization was disabled by the anti-thrashing governor, or non-LLM strategies reduced nothing).
4. **Aggressive backstop** (line 1052): if the result is *still* > `budget × 2`, every `ToolMessage` > `AGGRESSIVE_TRUNCATE_CHARS (1000)` chars is hard-cut.
5. **Summary self-truncation** (`_truncate_summary_messages`, line 957): any existing summary message (`lc_source == "summarization"`) longer than `SUMMARY_TOTAL_MAX_CHARS (16 000)` chars is re-truncated head 30% / tail 30%.
6. **Recovery injection** (`_inject_recovery_context`, line 922): the captured file-ops ratchet is rewritten into the summary's `## Relevant Files` section, so the checkpoint always carries an up-to-date read/modified file map.
7. **Bookkeeping** (`_record_compression`, line 635) and finally `request.override(messages=..., system_message=...)`.

Every failure mode is fail-open: if `_apply_compression` raises, the exception is logged (line 1217) and the original request proceeds unchanged — a broken compaction never breaks the turn.

## 📝 LLM Summary: Prompt, Chaining, Fallback

`_create_summary` / `_acreate_summary` (lines 768–816):

1. **Serialize** (`_serialize_for_summary`, line 167): each message becomes a tagged line — `[User]:` (≤ 2000 chars), `[Assistant]:` (≤ 2000 chars), `[Assistant tool call]: name(args ≤ 500 chars)`, `[Tool result|Tool error] (id):` (≤ 1800 chars + omission marker).
2. **Chain the prior checkpoint** (`_extract_previous_summary`, line 734): the newest `AIMessage` with `additional_kwargs["lc_source"] == "summarization"` is found and its `<summary>…</summary>` body extracted. If present, the prompt becomes `conversation + prior-summary + _SUMMARY_PROMPT_UPDATE` (carry objectives/constraints/decisions forward, newest wins conflicts, FIFO limits) instead of `_SUMMARY_PROMPT_FIRST`.
3. **Invoke** the auxiliary model with `config={"metadata": {"lc_source": "summarization"}}` so downstream tooling can identify summary calls.
4. **Guard rails:** a response that is empty or shorter than 50 chars falls back to the deterministic summary (line 785); any exception does the same (line 789). The LLM never gets the last word on failure.

The prompt template (`_SUMMARY_TEMPLATE`, line 99) fixes the Markdown skeleton — *Latest Unresolved User Request / Goal / Constraints & Preferences / Progress (Completed ≤ 5 · In Progress · Blocked) / Key Decisions ≤ 5 / Next Steps / Critical Context ≤ 3 / Relevant Files* — with "keep every section even when empty" and a secrecy rule ("NEVER include API keys, tokens, passwords, secrets"). `_enforce_fifo_limits` (line 283) re-imposes the item caps deterministically on the returned text, appending `"(N earlier items omitted for brevity)"`.

## 🧱 The Static Fallback (LLM-Free Summary)

`_build_static_fallback_summary` (line 198) produces the same section skeleton with zero model calls:

- last user request → *Latest Unresolved User Request*; first request → *Goal*;
- AI text containing decision keywords (`decided`, `choosing`, `because`, `therefore`) → *Key Decisions*, else *Completed*;
- every tool call → *Completed*; path-like tokens (contains `/` or `\`, or ends in `.py`/`.md`/…) → *Relevant Files* (≤ 10);
- error `ToolMessage`s → *Blocked* and *Critical Context*.

It is used verbatim when `skip_llm` is active, and as the safety net for short/failed LLM summaries.

## 🧹 Non-LLM Strategies

All three run in one pass (line 851) and respect `PROTECTED_TOOLS = {"memory", "skill_view", "skill_list"}`:

| Strategy | Module | Mechanism |
| :------- | :----- | :-------- |
| **Dedup** | `tool_output_dedup.py` | Collapse repeated identical tool outputs |
| **Prune** | `tool_output_prune.py` | Walk `ToolMessage`s **newest → oldest**, stop at a summary message or a `status="compacted"` result; skip protected tools; accumulate size (chars // 4) — every output beyond the newest `PRUNE_PROTECT_TOKENS (40 000)` tokens gets its content replaced by `[Old tool result content cleared]`. Applied only if total reduction ≥ `PRUNE_MIN_REDUCTION_TOKENS (5 000)` |
| **Target truncate** | `target_truncation.py` | Shrink oversized outputs toward `current_tokens × TARGET_TRUNCATE_RATIO (0.5)`: outputs ≥ `MIN_OUTPUT_CHARS_TO_TRUNCATE (500)` chars are cut down to `MAX_TOOL_OUTPUT_CHARS (2 000)` |

Preemptive truncation (before the pipeline, line 517) additionally caps every unprotected `ToolMessage` at 2000 chars with a head-30%/tail-30% keep and an `...[omitted N chars]...` marker.

## 📦 The Output: Summary Message Pair

`_build_new_messages` (line 822) wraps the summary text and emits exactly two messages:

```
[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted …
Respond ONLY to the latest user message that appears AFTER this summary.

<summary>
…summary Markdown…
</summary>

--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---
```

- **HumanMessage** `"What did we do so far?"` — a neutral question that keeps role alternation intact.
- **AIMessage** with `additional_kwargs={"lc_source": "summarization"}` — the marker that later turns use to (a) find and chain the prior checkpoint, (b) make pruning stop at the checkpoint, and (c) let comprehensive tests assert the summary is swallable from the model view once superseded.
- Total content is capped at `SUMMARY_TOTAL_MAX_CHARS (16 000)` with a head/tail 30/30 keep.

## 🛡️ Anti-Thrashing & Degradation Recovery

State lives in session-scoped `state_register_mem` under nine `summarization_*` keys, reset by `before_agent` each turn (line 1159).

**Compression governor** (`_should_skip_compression` / `_record_compression`, lines 613–662):

| Guard | Threshold | Effect |
| :---- | :-------- | :----- |
| Total attempts | `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5` | Stop compressing entirely for the session |
| Consecutive ineffective | `INEFFECTIVE_THRESHOLD = 2` | Set `skip_llm` — non-LLM strategies only |
| Effectiveness | message count reduced **or** token reduction ≥ `MIN_EFFECTIVENESS_PCT (0.05)` | Successful non-LLM strategies clear `skip_llm` again |

**Degradation monitor** (`_monitor_degradation`, line 988): after a compaction, if the model's reply has no text, a counter increments; at `DEGRADATION_NO_TEXT_THRESHOLD (3)` consecutive empty replies the middleware forces recovery — counters reset, `skip_llm` cleared, compression re-enabled — at most `MAX_RECOVERY_ATTEMPTS (2)` times. Any non-empty reply resets the counter. This catches the pathological "compact → model confused → empty output → compact again" loop.

## 🔄 System Prompt Refresh

Main agent only (`need_update_system_prompt=True`, lines 1068–1074): after a compression the persona files / long-term memory may have changed relevance, so the middleware reloads `memory_store` from disk, rebuilds the system prompt via `workspace.prompt_builder.build_system_prompt(session_id)`, writes it to **both** `state_register_mem` and `state_register_db` under `system_prompt`, and injects it with `request.override(system_message=SystemMessage(...))`. The outer `ContextEngineHook` picks the value up from the register on subsequent calls.

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

All thresholds live in `config/num.py`. Values marked ◆ are imported by the middleware; values marked ○ are defined but **not consumed** by it (see Honesty & Limitations).

| Constant | Value | Consumed where |
| :------- | :---- | :------------- |
| `PREEMPTIVE_TRUNCATE_RATIO` ◆ | `0.70` | preemptive gate — truncate-only threshold |
| `COMPRESSION_TRIGGER_RATIO` ◆ | `0.80` | preemptive gate — compact threshold; also builds both trigger clauses |
| `MIN_PRESERVE_TOKENS` ◆ | `2_000` | budget floor; budget without a context window |
| `MAX_PRESERVE_TOKENS` ◆ | `15_000` | budget ceiling |
| `PRESERVE_RATIO` ◆ | `0.25` | budget = 25% of context window |
| `PRUNE_PROTECT_TOKENS` ◆ | `40_000` | prune: newest tool-output tokens kept |
| `PRUNE_MIN_REDUCTION_TOKENS` ◆ | `5_000` | prune: minimum payoff to apply |
| `TARGET_TRUNCATE_RATIO` ◆ | `0.5` | target-truncate: shrink toward 50% of current tokens |
| `MIN_OUTPUT_CHARS_TO_TRUNCATE` ◆ | `500` | target-truncate: eligibility |
| `MAX_TOOL_OUTPUT_CHARS` ◆ | `2_000` | target-truncate: per-output cap |
| `AGGRESSIVE_TRUNCATE_CHARS` ◆ | `1_000` | aggressive backstop cut length |
| `SUMMARY_TOTAL_MAX_CHARS` ◆ | `16_000` | summary message char cap |
| `CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO` ◆ | `0.3` / `0.3` | all head/tail keeps |
| `DEGRADATION_NO_TEXT_THRESHOLD` ◆ | `3` | empty replies before forced recovery |
| `MAX_RECOVERY_ATTEMPTS` ◆ | `2` | forced-recovery budget |
| `MAX_TOTAL_COMPRESSION_ATTEMPTS` ◆ | `5` | governor: session attempt cap |
| `INEFFECTIVE_THRESHOLD` ◆ | `2` | governor: consecutive ineffective → skip LLM |
| `MIN_EFFECTIVENESS_PCT` ◆ | `0.05` | governor: token-reduction effectiveness |
| `PROTECTED_TOOLS` ◆ | `{"memory", "skill_view", "skill_list"}` | exempt from every shrink strategy |
| `LAST_TURN_RATIO_THRESHOLD` ◆ | `0.5` | last-turn compression gate |
| `COMPLETED_MAX_ITEMS` / `KEY_DECISIONS_MAX_ITEMS` / `CRITICAL_CONTEXT_MAX_ITEMS` ◆ | `5` / `5` / `3` | FIFO section caps |
| `FILE_OPS_LIST_MAX_CHARS` ◆ | `900` | file-ops ratchet list cap |
| `LATEST_USER_REQUEST_MAX_CHARS` ◆ | `800` | recovery-context request cap |
| `CHARS_PER_TOKEN` (estimator) | `4` | deterministic token estimate divisor |
| `SUMMARY_TRIM_TOKENS` ○ | `12_000` | imported by the middleware, never read |
| `AUTO_CONTINUE_PROMPT` ○ | — | imported by the middleware, never read |
| `DEGRADATION_MONITOR_COUNT` ○ | `5` | defined, not imported |
| `COMPRESSION_RESERVE_TOKENS` ○ | `16_000` | defined, not imported |
| `FILE_OPS_SECTION_MAX_CHARS` ○ | `2_000` | defined, not imported (only the 900-char list cap is used) |

## 🧪 Testing

| Suite | Covers |
| :---- | :----- |
| `tests/module/test_summarization_comprehensive.py` | 140-case module suite: trigger gates, budget/cutoff, FIFO caps, fallback, prune/dedup/target-truncate, degradation |
| `tests/integration/test_interrupt_marker_approach.py` | Marker semantics: the summary pair survives later compaction; `lc_source` on the `AIMessage`; last-turn compression |
| `tests/unit/test_pub_func_message_tools.py` | Estimator, prune (marker replacement, protect window, minimum-reduction gate) |
| `tests/module/test_summarization_trigger.py` | Production registration contract (uncapped window, 0.80 threshold) + low-token pass-through |
| `tests/integration/` hermetic e2e | Full-graph static-fallback compaction with zero network access |

The full process-isolated suite (`uv run python tests/run_tests_split.py`) passes with **2071 passed / 0 failed** (GROUP A 1384P/2S + GROUP B 687P/5D).

## ⚠️ Honesty & Limitations

- **`keep=("messages", 10)` is accepted but unused.** The constructor stores it for API compatibility; the actual tail retention is purely budget-based (`PRESERVE_RATIO` × context window, clamped to [2000, 15000]). Changing `keep` has no effect.
- **Doc-verbatim imports.** `json`, `hashlib`, `SUMMARY_TRIM_TOKENS`, and `AUTO_CONTINUE_PROMPT` are imported at the top of `summarization.py` but never read — they are transcribed together with the specification this file was written from and are covered by a lint-gate waiver. `DEGRADATION_MONITOR_COUNT`, `COMPRESSION_RESERVE_TOKENS`, and `FILE_OPS_SECTION_MAX_CHARS` are defined in `config/num.py` but consumed by nothing.
- **The estimator is `chars // 4`, not a tokenizer.** It is intentionally deterministic (reproducible tests, stable budgets) and calibrated for mixed English/code; CJK-heavy content will be under-counted (Chinese averages closer to 1–2 chars/token than 4).
- **Reported usage wins over the estimate.** When the last `AIMessage` carries `usage_metadata.total_tokens`, that number (which includes a full API-side count) drives triggering — the local estimate is the fallback only.
- **Compression is fail-open.** Any exception inside `_apply_compression` is logged and swallowed; the turn proceeds with the uncompressed history. A systematically broken auxiliary LLM therefore degrades to more frequent non-LLM shrinking, not to broken turns.
- **The static fallback is heuristic.** Keyword-based decision/completed classification and path extraction from raw tool args are best-effort; the section skeleton is guaranteed, the content quality is not.
- **`_SUMMARY_PREFIX`/`_SUMMARY_SUFFIX`/`<summary>` tags/`lc_source="summarization"` are load-bearing exact strings.** Later-turn chaining (`_extract_previous_summary`), prune stop-condition, and the test suites all match them literally — do not reword them casually.
