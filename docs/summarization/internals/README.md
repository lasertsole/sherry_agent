# 🧠 Summarization Internals — Truncate, Compact, Summary & Guards

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> Part of [Summarization](../README.md): the internal tracks — budget truncation & TTL, the compact pipeline, media offload, the LLM summary chain, the static fallback, the output pair, the anti-thrash guard matrix, the system prompt refresh, and the registration sites.

---

## ✂️ The Truncate Track: Budget Truncation & the TTL Module

Two truncation layers run inside `_run_budget_truncation` (overflow.py:219), in order:

**Step 1 — tool-call args** (`pub/func/message/tool_args_truncate.py`): every `AIMessage.tool_calls[].args` whose JSON serialization exceeds `MIN_ARGS_CHARS_TO_TRUNCATE (500)` chars — and whose tool is not in `PROTECTED_TOOLS` — is replaced with `{"_truncated_args": "head…[args truncated, omitted N chars]…tail"}` capped at `MAX_TOOL_ARGS_CHARS (2_000)` chars (head 30% / tail 30%, same ratios as tool results). This keeps `args` a dict (LangChain's `ToolCall.args` type), stays JSON-serializable for every provider adapter, and lets the model see the args were cut. The most recent `TRUNCATABLE_RECENT_SKIP (6)` messages are skipped and replaced `AIMessage`s are `model_copy` clones — tool_call_ids are never touched, so AIMessage↔ToolMessage pairing stays intact.

**Step 2 — tool results**: `pub/func/message/tool_result_ttl.py` provides the in-place truncation used by the truncate track. Design invariants (load-bearing):

- **In place only** — the module never removes, reorders or pops messages; it only mutates `msg.content` (or a content-list block) and returns indices. This preserves the tool-call/`ToolMessage` pairing that the provider API and `ToolCallNormalize` depend on.
- **Non-empty placeholders** — a truncated result always keeps non-empty content: `ToolCallNormalize.before_model` sanitizes the transcript by **dropping empty `ToolMessage`s**, so an empty placeholder would silently break the pairing.
- **30% head / 30% tail keep** (`CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO`) with an omission marker.

What the middleware actually consumes: `truncate_tool_args` (step 1, args) and **`truncate_to_budget`** (step 2, tool results), driven by the router's candidate list — `_run_budget_truncation` (overflow.py:219) truncates candidates until the budget (`usable × TRUNCATE_BUDGET_RATIO`) is met. Because step 1 returns new `AIMessage`s instead of mutating, the function returns the final list and every caller MUST feed that list into `request.override`.

**read_file results stay recoverable**: the head+tail clip in `pub/func/message/target_truncation.py` (run by the non-LLM strategies, `_run_non_llm_strategies`) resolves each `ToolMessage` back to its AIMessage tool call by `tool_call_id`; when the tool is `read_file` and `args.file_path` is set, the clipped middle is replaced with a recovery notice instead of the anonymous marker. The notice keeps the same 30% head / 30% tail ratios, names the original `file_path`, and states `Use offset=<N> to continue reading: read_file(file_path='<path>', offset=<N>, limit=500)`. `N` is the **absolute, 1-based** file line of the first line not fully retained in the head — a page read with `offset=100` therefore resumes past wherever the head actually stopped, and a line cut mid-way is re-read, never skipped. When the offset cannot be derived (the payload is not a read_file JSON result), the notice asks for a restart from `offset=1` instead of guessing. Every other tool keeps the anonymous `...[truncated N chars]...` marker byte-for-byte.

The TTL registry itself (`record_first_seen` / `select_expired` / `truncate_expired`, `PRUNE_TTL_SECONDS = 300`, `TTL_REGISTRY_MAX_ENTRIES = 512`, keyed by `tool_call_id`, volatile across restarts) is exercised **only by the test suite** today — the middleware has no age-based expiry wired in (see [Honesty & Limitations](../README.md#%EF%B8%8F-honesty--limitations)).

## 🔁 The Compact Track: Inside `_apply_compression`

`_apply_compression` (compression.py:85; async twin compression.py:98) runs, in order:

1. **Capture recovery context** (`_capture_recovery_context`, compression.py:363): the last user request (≤ 800 chars) and the file-operations ratchet — paths extracted from `read`/`write`-family tool calls (summary_generation.py:390), merged with the previous round's set (reads are remembered, modified files are never downgraded to read-only).
2. **Non-LLM strategies** (`_run_non_llm_strategies`, compression.py:314): `dedup → prune → target truncate → tool-args truncate` (details in the [truncate track](#-the-truncate-track-budget-truncation--the-ttl-module)). These are free — no model call.
3. **LLM-or-not decision**:

   ```
   if tokens_after_non_llm > budget × 2  OR  skip_llm  OR  nothing was reduced:
       summarize [0:cutoff] and rebuild   → strategy "llm_summary" / "fallback"
   else:
       keep as-is                          → strategy "non_llm_sufficient"
   ```

   Non-LLM shrinking is given the first chance; the auxiliary LLM is only spent when the history is still more than twice the preserve budget (or LLM summarization was disabled by the governor, or non-LLM strategies reduced nothing).
4. **Aggressive backstop** (`_aggressive_truncate`, compression.py:360): if the result is *still* too big, every `ToolMessage` > `AGGRESSIVE_TRUNCATE_CHARS (1 000)` chars is hard-cut with a marker — and so is every tool-call args JSON beyond the same cap (head-only, `PROTECTED_TOOLS` exempt, replaced with `{"_truncated_args": ...}`).
5. **Summary self-truncation** (`_truncate_summary_messages`, compression.py:417): any existing summary message (`lc_source == "summarization"`) longer than `SUMMARY_TOTAL_MAX_CHARS (16 000)` chars is re-truncated head 30% / tail 30% (`_truncate_content`, compression.py:414).
6. **Recovery injection** (`_inject_recovery_context`, compression.py:379): the captured file-ops ratchet is rewritten into the summary's `## Relevant Files` section, so the checkpoint always carries an up-to-date read/modified file map.
7. **Bookkeeping** (`_record_compression`, thrash.py:117) and finally `request.override(messages=..., system_message=...)`.

### 💾 Compression-time nudges

Message persistence runs outside the compression path: human/AI messages are flushed to MesMemory at every model boundary and tool results the moment they return, through `MessagePersistenceMiddleware` (`agent/middlewares/message_persistence/`), write-once via the persistent `persisted_message_ids` watermark. A compact only compacts and schedules the nudge dispatch below. See `agent/middlewares/README.md` for the middleware's trigger semantics.

**Compression-time nudges** (`agent/middlewares/summarization/nudges.py::schedule_compression_nudges`): the memory review (`_nudge_memory`) is dispatched on every compression; plan extraction evaluates `_detect_todo_all_complete` at the same point. Both dispatch fire-and-forget under the NUDGE lane, so they can never block the model call. While a nudge lock is held the compression skips dispatch entirely (nothing is queued). The single-fire `nudge_plan_extraction_fired` flag allows one extraction per completion cycle, so a session that never compresses never fires plan extraction.

**Cutoff selection** (`_determine_cutoff`, compression.py:277): split the history into turns, walk **from the newest backwards** accumulating against the preserve budget `clamp(window × 0.25, 2 000, 15 000)` (`_calculate_preserve_budget`, compression.py:70); a turn that does not fully fit is split mid-turn. `_adjust_for_orphan_pairs` (compression.py:311) then walks the cutoff backwards until no `ToolMessage` is separated from its `AIMessage` tool-call. Unless the last-turn ratio gate fires (last user turn ≥ `LAST_TURN_RATIO_THRESHOLD (0.5)` of tokens — `_check_last_turn_ratio`, called at wrap entry core.py:421 / core.py:503), the cutoff never crosses the last `HumanMessage`.

Every failure mode is fail-open: if `_apply_compression` raises, the exception is logged and the original request proceeds unchanged — a broken compaction never breaks the turn.

## 🖼️ Compression-Time Media Offload (Inline Media → Reference)

Compression must never hand a base64 payload to the auxiliary summarizer. Before the summarized prefix (`current_messages[:cutoff]`) is serialized, `offload_inline_media` (`agent/middlewares/summarization/media_offload.py`) rewrites every inline media block in **that range only**:

- a `data:` URL / base64 payload (`image_url` / `audio_url` / `video_url`, a bare `base64` field, `audio_bytes` / `video_bytes`, or an Anthropic-style `source.data`) is decoded and written to `SESSIONS_DIR/<session_id>/media/{sha256(raw)[:16]}{ext}`; the extension is derived from the magic bytes via `media_handlers._infer_extension`;
- identical bytes are written once — the content-hash filename deduplicates repeats within and across compactions;
- the block is replaced by a text pointer `[evicted to: <path>]`, the same marker `pub/func/message/eviction.py` emits, so `_collect_evicted_refs` picks it up and `_finalize_summary_doc` carries it into `SummaryDoc.evicted_refs` (rendered as *Evicted References*);
- a block that cannot be decoded or written becomes `<media error="failed_to_offload" />` — a media failure never crashes compression.

The preserved tail window (`current_messages[cutoff:]`) is never touched: media stays resident until its turn is actually summarized. Both compression paths call the offload — `_apply_compression_under_lock` (sync) and `_aapply_compression_under_lock` (async) — on the prefix slice, before the nudge scheduling and the memory flush. The summary prompt (`_SUMMARY_JSON_RULES` / `_SUMMARY_TEMPLATE`) tells the model the pointers exist, that they must be carried into `evicted_refs` verbatim, that it must not invent visual / audio / video details, and that the payload can be retrieved from the path.

Media files live in the session tree, so `clear_session()` removes them together with `evicted/` and `plans/`.

## 📝 LLM Summary: Prompt, Chaining, Fallback

`_create_summary` / `_acreate_summary` (summary_generation.py:710 / summary_generation.py:758):

1. **Serialize** (`_serialize_for_summary`, summary_generation.py:207): each message becomes a tagged line — `[User]:` (≤ 2 000 chars), `[Assistant]:` (≤ 2 000 chars), `[Assistant tool call]: name(args: > 500 chars → head 300 + tail 150 + omission marker)`, `[Tool result|Tool error] (id):` (> 2 000 chars → keep 1 800 + omission marker).
2. **Chain the prior checkpoint** (`_extract_previous_doc` / `_extract_previous_summary`): the newest `AIMessage` with `additional_kwargs["lc_source"] == "summarization"` is read as the structured `summary_doc` payload (rendered back to Markdown for `<prior-summary>`); a message without the payload — legacy sessions, or a round that fell back to free-form — is parsed from its `<summary>…</summary>` body instead. With a prior document the prompt becomes `conversation + <prior-summary-json> + _SUMMARY_PROMPT_UPDATE_STRUCTURED`; legacy Markdown goes in through `<prior-summary>` unchanged, and the first compressed round after an upgrade already outputs a new `SummaryDoc` (no migration).
3. **Structured output** (`summary_doc.py::SummaryDoc`): the auxiliary model is wrapped with `with_structured_output(SummaryDoc, method="json_mode")`. The configured `glm-5.3-flash` endpoint ignores function-call schemas (the default method returns free text that the Pydantic parser rejects — verified live), so `json_mode` is the primary tier; a parse/validation failure degrades to a raw call parsed by `json_repair` (`_sync_json_repair_doc` / `_async_json_repair_doc`); if that also fails, the legacy free-form Markdown prompt is the last LLM tier, and the static fallback stays the final guard.
4. **Invoke** the auxiliary model with `config={"metadata": {"lc_source": "summarization"}}` so downstream tooling can identify summary calls.
5. **Guard rails:** a free-form response that is empty or trivially short falls back to the deterministic summary; any exception does the same. The LLM never gets the last word on failure.

**Render (Doc → Markdown).** `render_summary_markdown` is pure and deterministic (same document → same bytes, prefix-cache safe). It emits the legacy section skeleton and appends *Active Plan Notes* / *Evicted References* only when those arrays are non-empty:

| `SummaryDoc` field | Rendered section | Cap (code-layer) |
| :--- | :--- | :--- |
| `latest_user_request` | `## Latest Unresolved User Request` | verbatim, uncapped |
| `goal` | `## Goal` | — |
| `constraints` | `## Constraints & Preferences` | — |
| `completed` | `### Completed` | `completed[-5:]` |
| `in_progress` / `blocked` | `### In Progress` / `### Blocked` | — |
| `key_decisions` | `## Key Decisions` | `key_decisions[-5:]` |
| `next_steps` | `## Next Steps` | — |
| `critical_context` | `## Critical Context` | `critical_context[-3:]` |
| `relevant_files` | `## Relevant Files` | — |
| `active_plan_notes` | `## Active Plan Notes` (non-empty only) | `active_plan_notes[-20:]` |
| `evicted_refs` | `## Evicted References` (non-empty only) | `evicted_refs[-20:]` |

The caps are array slices in `cap_summary_doc` (what gets stored on the chain); the renderer slices again for display and appends `"(N earlier items omitted for brevity)"`. `evicted_refs` is maintained by code: `_collect_evicted_refs` scans the summarized range for `[evicted to: <path>]` markers and the human-message `lc_evicted_to` tag, and `_finalize_summary_doc` merges the prior document's entries with the new ones (deduplicated, in order). `_inject_recovery_context` rewrites the file-ops ratchet into both the rendered `## Relevant Files` section and the stored document's `relevant_files` field.

### 🗂️ Active Plan Notes & plan context injection

Compression is plan-aware. Before the auxiliary call, `_get_plan_context_sync(session_id)` (`core.py`, beside the TaskFlow block) renders the plan this session is executing as an authoritative prompt block:

- plan-active detection reuses the two raw association sources of `agent/tools/todolist/knowledge/ownership.py` — the session's `plan_ref` state key and a `plan_ref` on one of this session's todos (`plan_context.py::resolve_active_plan`); the boulder source is deliberately excluded;
- the injected block is **plan file relative path + plan name + open-todo summary** — never the plan body (the model can `read_file` the path). A session whose todos are all `completed` / `cancelled` is **not** plan-active;
- the block is injected on both prompt paths (first summary and chained update, structured and legacy free-form).

`SummaryDoc.active_plan_notes` is owned by the pipeline, not the model, so plan-scoped lessons survive for as long as the plan is active:

- on a chained update the previous document's entries are inherited **verbatim, in order**; the model may only append new one-line lessons (`symptom -> avoidance`);
- the array is capped at `active_plan_notes[-20:]`; the chain render appends `(N earlier items omitted for brevity)` and the stored payload keeps the capped tail;
- once the plan finishes (all todos `completed` / `cancelled`) the resolver reports no active plan: the render drops the `## Active Plan Notes` section and the next document's array is cleared. A session with no `plan_ref` at all likewise never carries the array.

**Latest user request verbatim + eviction pointer.** The *Latest Unresolved User Request* section is never truncated: `latest_user_request` is quoted verbatim (the `max 800 chars` instruction is gone from the legacy template too), and the serialized `<conversation>` comes from **state** — for an evicted human message state keeps the full text plus the `lc_evicted_to` tag while only the model view is a preview. When the latest user request itself was evicted, the pipeline appends its `[evicted to: <path>]` pointer to the field (code-owned, `_latest_human_eviction_ref`), so the summary chain always keeps the way back to the full text on disk; internal injections (`metadata.internal`) never steal that anchor.

**Chained-summary filtering** (`_filter_summary_messages`): when a previous checkpoint exists, its Human/AI pair is stripped from the serialized `<conversation>` input — the extracted prior summary is injected only through `<prior-summary>` (or `<prior-summary-json>`), so the old summary text appears exactly once in the prompt. Both halves of the pair carry `additional_kwargs={"lc_source": "summarization"}`, which also keeps both out of MesMemory (`MessagePersistenceMiddleware._is_persistable` + `HumanMessageRowBuilder`): the pair is an internal compaction artifact, not conversation history. The filter runs after `_extract_previous_doc` / `_extract_previous_summary` (chaining still sees the old summary), and the filtered list feeds the serialization, the prompt, and both static-fallback branches (LLM failure / short response) — including the `skip_llm` path in `_apply_compression_under_lock` / `_aapply_compression_under_lock`. Aligns with opencode-dev's `hidden` set and deepagents' `_filter_summary_messages`.

The legacy prompt template (`_SUMMARY_TEMPLATE`) still fixes the free-form fallback skeleton — *Latest Unresolved User Request / Goal / Constraints & Preferences / Progress (Completed ≤ 5 · In Progress · Blocked) / Key Decisions ≤ 5 / Next Steps / Critical Context ≤ 3 / Relevant Files* — with "keep every section even when empty" and a secrecy rule ("NEVER include API keys, tokens, passwords, secrets"). The structured path replaces the Markdown skeleton with `_SUMMARY_JSON_RULES` (the JSON field list, same secrecy rule); the field semantics live on the Pydantic `SummaryDoc` model itself.

**User-request source identification.** Every persisted `human` row carries an `origin`, stamped at the transport entry: `"user"` for WS/channel user input, `"task_intent"` for orchestrator steering injections, `"subagent_completion"` for completion carriers, `"cron"` for cron-delivered turns (AI/tool rows keep `NULL`, and rows written before origin tagging read back as legacy user messages). The *Latest Unresolved User Request* source is positively identified by that column: only user-origin messages (`origin = 'user'`, or legacy `NULL`) are user requests — internal injections (`task_intent` / `subagent_completion` / `cron`) are never quoted as one. The plural `unresolved_user_requests[]` list of the routing plan uses the same positive filter.

## 🧱 The Static Fallback (LLM-Free Summary)

`_build_static_fallback_summary` (:296) produces the same section skeleton with zero model calls:

- last user request → *Latest Unresolved User Request*; first request → *Goal*;
- AI text containing decision keywords (`decided`, `choosing`, `because`, `therefore`) → *Key Decisions*, else *Completed*;
- every tool call → *Completed*; path-like tokens (contains `/` or `\`, or ends in `.py`/`.md`/`.js`/`.ts`/`.json`) → *Relevant Files* (≤ 10, `http` links excluded);
- error `ToolMessage`s → *Blocked* and *Critical Context*.

It is used verbatim when `skip_llm` is active, and as the safety net for short/failed LLM summaries.

## 📦 The Output: Summary Message Pair

`_build_new_messages` (summary_generation.py:806) wraps the summary text and emits exactly two messages:

```
[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted …
Respond ONLY to the latest user message that appears AFTER this summary.

<summary>
…summary Markdown…
</summary>

--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---
```

- **HumanMessage** `"What did we do so far?"` — a neutral question that keeps role alternation intact; it carries the same `lc_source` marker as its AI half.
- **AIMessage** with `additional_kwargs={"lc_source": "summarization"}` — the marker that later turns use to (a) find and chain the prior checkpoint, (b) strip the pair from a chained re-summarization input and make pruning stop at the checkpoint, and (c) let tests assert the summary is swallable from the model view once superseded.
- The pair never enters MesMemory: `MessagePersistenceMiddleware._is_persistable` skips both `lc_source="summarization"` halves (the human row builder applies the same gate).
- The AIMessage also carries the structured document itself: `additional_kwargs["summary_doc"]` (the capped `SummaryDoc` as a plain JSON-serializable dict — the chain carrier, fed back as `<prior-summary-json>`). Legacy/free-form rounds omit it and `_extract_previous_summary` falls back to the `<summary>` body.
- Total content is capped at `SUMMARY_TOTAL_MAX_CHARS (16 000)` with a head/tail 30/30 keep.

## 🛡️ Anti-Thrash Guard Matrix & Degradation Recovery

State lives in session-scoped `state_register_mem` under **thirteen** `summarization_*` keys (state_aliases.py:17–24). `_reset_turn_state` (thrash.py:157) resets **eleven** of them at every turn start; `summarization_last_user_question` and `summarization_cooldown_rounds` are deliberately **not** reset per turn.

| Guard | Key | Threshold | Effect |
| :---- | :-- | :-------- | :----- |
| Turn cooldown | `summarization_cooldown_rounds` | `COMPACTION_COOLDOWN_ROUNDS = 3` | Armed after every actual compact (core.py:345); ticked down by **every** model call (thrash.py:87); blocks T1 compact routes, T2 proactive and T3 — never the T4/T5 forced ring |
| Per-turn compactions | `summarization_turn_attempts` | `MAX_COMPRESS_ATTEMPTS_PER_TURN = 3` | Incremented by core.py:345; suppresses T2 proactive + T3 (forced ring exempt) |
| Overflow retries (T4/T5 shared) | `summarization_overflow_retries` | `MAX_OVERFLOW_RETRIES = 3` | Shared by both error classes and reset per turn; incremented after each successful forced step; exhausted → original provider error propagates |
| Session compressions | `summarization_compression_count` | `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5` | `_should_skip_compression` (thrash.py:110) returns True — proactive compression stops entirely |
| Consecutive ineffective | `summarization_compression_ineffective` | `INEFFECTIVE_THRESHOLD = 2` | Sets `skip_llm` — non-LLM strategies only |
| Effectiveness | (`_record_compression`, thrash.py:117) | message count reduced **or** token reduction ≥ `MIN_EFFECTIVENESS_PCT (0.05)` | Successful non-LLM strategies (`dedup`/`prune`/`truncate`/`fallback`/`aggressive`) clear `skip_llm` again |
| Degradation recovery budget | `summarization_recovery_attempts` | `MAX_RECOVERY_ATTEMPTS = 2` | Caps forced recoveries from the degradation monitor |

**Degradation monitor** (`_monitor_degradation`, thrash.py:131): only consulted when a compaction actually happened this call (`_compaction_just_happened` flag). If the model's reply has no text, a counter increments; at `DEGRADATION_NO_TEXT_THRESHOLD (3)` consecutive empty replies — and while `summarization_recovery_attempts < 2` — it sets `force_recovery`, clears the ineffective streak and the session compression count. Any non-empty reply resets the counter. This catches the pathological "compact → model confused → empty output → compact again" loop. Note the interplay: the forced flag is read at wrap entry (core.py:426) **before** `_should_skip_compression`, and the skip gate consumes it by resetting the counters and proceeding (thrash.py:110–115) — recovery compression runs exactly once.

## 🔄 System Prompt Refresh

Main agent only (`need_update_system_prompt=True`): after a compression the middleware rebuilds the system prompt and writes it to the `system_prompt` state key, so the next model call sees persona files / long-term memory as they are now. Two delivery paths: `request.override(system_message=SystemMessage(...))` directly after compaction, and — when a T1 compact already happened but the anti-thrash gate blocks a second one — the rebuilt prompt is still delivered in the gate path (core.py:441–459), because chains without the `@dynamic_prompt` system-prompt middleware (subagent / nudge pipelines) rely on this middleware delivering it. On the gate path the rebuild is injected **only when the request's current system message differs**: if the content already matches, no `override` and no new `SystemMessage` are created (the prompt is not re-injected).

## 📌 Registration Sites

```python
# agent/core.py:204 — main agent (Summarization is the LAST middleware:
# innermost wrap layer, closest to the LLM)
Summarization(
    need_update_system_prompt=True,
    model=auxiliary_llm,
    main_llm_context_window=main_llm_max_tokens,
    trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
    keep=("messages", 10),
)

# agent/tools/subagent/spawn/core.py:909 — worker agent (first middleware)
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
