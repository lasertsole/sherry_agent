# 🧭 Context Governance: Persistence, Eviction, Slices & Overflow Clip

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> How raw history is kept durable and the model-visible context is kept small: write-once persistence at every model boundary and tool return, tool-result eviction to disk with a recoverable preview, execution-time `read_file` slicing, human-message eviction with a full-text state, a no-LLM overflow tail clip that runs before any compression route, and chain-summary filtering that keeps old summaries out of the conversation payload.

Every message the agent produces is valuable twice: once as **raw history** (what actually happened, for search and for compression) and once as **model context** (what fits in the window right now). This page documents the six mechanisms that reconcile those two needs — they all share one rule: **never lose data, only shrink the model view, and always leave a pointer back to the full text.**

**Source of truth:** `agent/middlewares/context_eviction/core.py`, `agent/middlewares/message_persistence/core.py`, `agent/middlewares/message_persistence/prepare.py`, `pub/func/message/eviction.py`, `pub/func/message/overflow_clip.py`, `pub/func/message/target_truncation.py`, `pub/func/message/tool_args_truncate.py`, `agent/middlewares/summarization/core.py`, `context_engine/store/core.py`, `config/features/agent_side/tool_result_eviction.py`, `config/features/agent_side/summarization.py`. Every claim below was checked against that code.

## 🎯 Overview & Pipeline

The mechanisms form one pipeline; each stage shrinks the model view a little further, and none of them destroys the raw record:

```text
tool returns
  │  ContextEvictionMiddleware.wrap_tool_call
  │    generic tool, text > 20 000 chars → full text to evicted/, head+tail preview in state (P0-2)
  │    read_file                         → execution-time 4 000-char head slice, no file written (P2-4)
  ▼
graph state (tool results: preview only · human messages: full text + lc_evicted_to tag)
  │
  │  MessagePersistenceMiddleware
  │    wrap_tool_call  → flush the RAW result the moment the handler returns
  │    after_model     → flush new human/ai/tool messages at every model boundary
  ▼
MesMemory (full text; persisted_message_ids watermark = write-once)
  │
  │  context pressure triggers Summarization (T1–T5)
  │    1. tail clip      — no LLM: stub the trailing contiguous ToolMessage batch (P1-2)
  │    2. existing route — truncate_tool_results_only / compact_only / compact_then_truncate
  │    3. forced recovery (T4/T5 provider errors) — clip first, then compact + budget truncate, then retry
  ▼
session end → clear_session() removes the session folder (evicted/ + plans) and the watermark
```

| Stage | Mechanism | LLM cost | Effect on the model view |
|---|---|---|---|
| **Tool return** | `ContextEvictionMiddleware` (P0-2 / P2-4) | none | generic > 20 000 chars → head/tail preview + file pointer; `read_file` → 4 000-char slice + notice |
| **Model boundary** | `MessagePersistenceMiddleware` `after_model` | none | full text written to MesMemory (state untouched) |
| **Human message** | `ContextEvictionMiddleware` (P1-9) | none | request view truncated to a preview; state/MesMemory keep the full text |
| **Overflow (first move)** | `clip_overflow_tail` (P1-2) | none | trailing tool results stubbed; message identity and pairing intact |
| **Overflow (existing routes)** | `summarization` 4-route dispatch | one auxiliary-LLM call on compact routes | truncation and/or history compaction |
| **Overflow (provider error)** | T4/T5 forced recovery | one call per compact step | clip → compact + budget truncate, retried up to 3 times |

## 🗂️ Information Sources

Everything that reaches graph state or MesMemory enters from one of the sources below. The `origin` column is being upgraded into a full-coverage source marker: `NULL` is a legacy user row written before tagging (the read side treats it as `user`), and a message carrying `internal=True` is **not a user request** — the summary's Unresolved list accepts only user-sent messages (positive identification). Non-message sources (eviction files, plan knowledge) are listed too: they never become a `messages` row but are still injectable context. Rows marked `planned` / `reserved` are not implemented yet.

| Information source | origin / marker | internal | Produced when | Persistence | Injection behavior |
|---|---|---|---|---|---|
| Frontend WS user message | `origin='user'` | — | the user sends a message in the client | `messages` row `origin='user'` + full text (flushed at every boundary) | resident in state/MesMemory; the model view may be evicted to a preview; the summary keeps the latest user request verbatim (`latest_user_request`; a multi-request list + per-request eviction pointer: `planned`) |
| Channel user message (QQ, …) | `origin='user'` | — | the user sends through a channel adapter | same as above | same as above |
| TaskIntent steering / reminder | `origin='task_intent'` | `True` | plan-active steering / task-intent arming (`task_intent/core.py::_task_intent_message`) | `messages` row | **not a user request** — excluded from the Unresolved list |
| Subagent completion carrier | `origin='subagent_completion'` | `True` | a background subagent finishes and announces its result | `messages` row (origin stamped at the persistence seam, `context_engine/store/core.py`) | not a user request; visible to the model view |
| Heartbeat-triggered turn | `origin='heartbeat'` | — | heartbeat-service turn (**no such path today — `reserved`**) | — | not a user request |
| Cron-triggered turn | `origin='cron'` | `True` | a scheduled job's session turn (`origin_for_source`) | `messages` row | not a user request |
| Compression summary pair | `lc_source='summarization'` (in `additional_kwargs`, not the origin column) | — | a compaction artifact (`_build_new_messages`) | **never persisted to MesMemory**; a state summary pair | `<summary>` resident in the model view; carried forward as `<prior-summary>` |
| Eviction file | non-message — disk file | — | P0-2 / P1-9 eviction | `SESSIONS_DIR/<session_id>/evicted/` (byte-exact full text) | on-demand `read_file`; the summary chain carries `evicted_refs[]` pointers in the structured summary doc |
| Plan knowledge | non-message — disk directory | — | plan extraction | `workspace/knowledge/plans/<plan_key>/` | injected as a `<knowledge>` block via `plan_ref` |
| FACTS.md | `workspace/memory/FACTS.md` (memory tool target `facts`) | — | broad, module-independent pitfalls and conventions: compression-time memory review + completed-plan extraction | memory file (1 375-char limit; overflow drops the oldest entries first) | injected into every system prompt as a resident FACTS memory block |

## 💾 Persistence at Every Boundary

`agent/middlewares/message_persistence/core.py` (`MessagePersistenceMiddleware`) is the persistence floor the whole page rests on. It writes at **two timings**:

| Message | Persisted at |
|---|---|
| `ToolMessage` | **the moment the tool handler returns** (`wrap_tool_call` / `awrap_tool_call`) |
| `HumanMessage` | the turn's first model boundary (`after_model` / `aafter_model`) |
| `AIMessage` (including its `tool_calls`) | the boundary right after the model produced it |
| HITL denial (`status="error"` ToolMessage) | the next model boundary — HITL wraps this middleware from the outside, so its short-circuit never reaches the flush |

Both timings share one batch pipeline: role/marker filter → watermark → HITL-denial re-pairing + tool-result dedup → write → mark. Details that matter:

- **`persisted_message_ids` watermark (write-once).** Before writing, `filter_persisted_message_ids` drops candidates whose lookup keys are already tombstoned for the session; after writing, `mark_message_ids_persisted` tombstones every candidate handed to the writer. The watermark is a SQLite table (`context_engine/store/db.py`), so it survives restarts.
- **id + fingerprint lookup.** A tool result is persisted on return **before** the graph reducer assigns it an id; the watermark lookup therefore checks both the LangGraph message id and a `sha1:` content fingerprint (role + content + `tool_call_id`). The next boundary, and a post-restart replay, both match the same logical message on one of the two keys (`_watermark_lookup_keys` in `prepare.py`).
- **Summary pairs are never persisted.** `_is_persistable` drops every message tagged `additional_kwargs["lc_source"] == "summarization"` (the AI half of the pair), and `HumanMessageRowBuilder.build` (`context_engine/store/core.py`) returns `None` for the human half. Compression artifacts are prompt scaffolding, not conversation history.
- **Fail-open.** A missing `session_id` skips silently; a writer error is logged and NOT tombstoned, so the same batch is retried at the next boundary. Persistence can never break a turn or a tool result.

This is why the rest of the page can be aggressive: **every payload is already durable before any later stage shrinks it.**

## 🗜️ Tool-Result Eviction (P0-2)

`ContextEvictionMiddleware.wrap_tool_call` intercepts the tool response **before it reaches graph state** (`agent/middlewares/context_eviction/core.py`; primitives in `pub/func/message/eviction.py`):

- A generic result whose extracted text is **over `evict_threshold_chars` (20 000 chars)** is written to `SESSIONS_DIR/<session_id>/evicted/<tool_call_id>_<md5[:8]>.txt`, and the message content is replaced by a head/tail preview carrying the file path.
- `excluded_tools` (8 names — `read_file`, `write_file`, `patch_file`, `search_files`, `list_files`, `memory`, `skill_view`, `skill_list`) pass through untouched: their payload already lives on the backend filesystem or is cheap to recover. `read_file` additionally takes the slice path below.
- The replacement is built with `ToolMessage.model_copy`, so the message `id`, `tool_call_id`, `name`, `status`, and `additional_kwargs` survive — pairing, sanitizer, and watermark all keep matching the same logical message.
- Multimodal non-text blocks (images / audio / video) are preserved verbatim; only the text block is replaced.
- Safety guards: an unsafe `session_id` (empty / `.` / `..` / separator) skips without touching disk; a message already carrying the `[evicted to: …]` marker is never evicted twice; a preview that would not be smaller than the original (one giant line) is skipped.

Preview format (`pub/func/message/eviction.py`, `preview_head_lines = preview_tail_lines = 5`):

```text
[evicted to: <path>]
--- head (5 lines) ---
<first 5 lines>
...
--- tail (5 lines) ---
<last 5 lines>
[full content: N chars, evicted at <ts>]

Use read_file(file_path='<path>', offset=0, limit=100) to read the full content in chunks.]
```

**The three stores** — where each copy lives after eviction:

| Where | What it holds |
|---|---|
| graph state / checkpointer / next model call | the **preview** only |
| MesMemory (`messages` table) | the **full text**, written by the inner persistence flush the moment the tool returned |
| `SESSIONS_DIR/<session_id>/evicted/` | a byte-identical copy; `load_evicted()` / `read_file` retrieves it |

The ordering that makes this work: in the `wrap_tool_call` chain, `MessagePersistenceMiddleware` is **innermost** and `ContextEvictionMiddleware` sits **outside** it, so the raw result is flushed first and only the preview travels on to state. The watermark is covered either way: `model_copy` carries the inner flush's in-process `_db_persisted` marker, and when the raw write succeeded the replacement's watermark key is additionally tombstoned (`_cover_with_watermark`), which covers a restart where the marker is lost and the preview's fingerprint no longer matches the raw one. When the raw write failed, nothing is tombstoned and the next boundary retries with the preview content.

## ✂️ `read_file` Slice (P2-4)

`read_file` results are **not** offloaded — the file already lives on disk, so writing a second copy would be pure duplication. `slice_read_file_result` (`pub/func/message/eviction.py`) instead replaces the content with the first **`_READ_FILE_SLICE_CHARS` (4 000) characters** plus a recovery notice (`"...[Output was truncated due to eviction threshold. Use read_file with offset and limit to retrieve specific portions.]"`). No eviction file is written, and the helper is idempotent: an already-sliced result (notice present) is returned untouched.

This is the **execution-time** half of a two-stage reduction. The **compression-time** half lives in `pub/func/message/target_truncation.py::_truncate_read_file_content`: when compression clips context, it resolves each `ToolMessage` back to its `read_file` call by `tool_call_id` and keeps head 30 % + tail 30 % of `max_tool_output_chars` (2 000), replacing the middle with a recovery notice that carries the **parser-derived, 1-based continuation offset** (`Use offset=<N> to continue reading…`; falls back to "re-read from the start" when the payload does not parse).

The two stages are complementary by construction: when compression later clips an execution-sliced payload, the truncated JSON no longer parses, so the compression notice deterministically falls back to the restart form — no incorrect offset is ever emitted, and the execution-time slice helper never re-slices its own output.

## 📥 Human-Message Eviction (P1-9)

A user can paste a payload no tool produced: logs, documents, transcripts, whole codebases. `ContextEvictionMiddleware` ports DeepAgents' human-message eviction with a Sherry-specific split.

- **Trigger** (`before_model` / `abefore_model`): `human_evict_enabled` AND the **last** message is a `HumanMessage` AND it carries no `lc_evicted_to` AND its extracted text is **over `human_evict_threshold_chars` (200 000 chars)**. Only the last message is ever examined, so a past user turn is never revisited.
- **Tag + offload** (`evict_human_message`): the full text is written to `SESSIONS_DIR/<session_id>/evicted/human-<msg_id|timestamp>.md`, then the hook returns a partial state update `{"messages": [tagged]}` where `tagged` has the **same id and content** and only adds `additional_kwargs["lc_evicted_to"]`. The standard `add_messages` reducer updates the message **in place by id** — no message-list rewrite, no `DeltaChannel` dependency, no prefix-cache invalidation from the state side. The file is written before the tag is produced, so a failed write never yields a dangling pointer.
- **Model view** (`wrap_model_call` / `awrap_model_call`): every `HumanMessage` carrying `lc_evicted_to` is replaced **in the request only** (`request.override(messages=...)`) by a preview built from the state text: eviction path, head/tail 5 lines each, and a `read_file` recovery notice. Non-text blocks (images / audio / video) are preserved verbatim (`_build_evicted_content`) — media is never offloaded into a text file.
- **Self-heal**: if the eviction file is missing (session dir pruned, disk issue), `_heal_eviction_file` rewrites it from the state text at the next model call — but only when the target is the session's own `evicted/` directory; a foreign path in the tag is refused with a warning.

### Why the Split Differs from Tool Results

| Where | Tool results (P0-2) | Human messages (P1-9) |
|---|---|---|
| graph state / checkpointer | preview only | **full text** + the `lc_evicted_to` tag |
| MesMemory (`messages` table) | full text | **full text** (the tag does not filter persistence) |
| next model call (request view only) | preview | preview (path + `read_file` hint) |
| `SESSIONS_DIR/<session_id>/evicted/` | byte-identical copy | byte-identical copy |

The asymmetry follows from *when* the message is persisted. A tool result is flushed by the inner persistence layer at tool return, so state can safely hold the preview. A human message is persisted at the turn's first `after_model` boundary — if state held only the preview, MesMemory would archive the preview and both `message_search` and compression would lose the real text. So state keeps the full text and only the request view is truncated. Because the message id never changes, the watermark is untouched and no second row is written.

## ⚡ Overflow Tail Clip (P1-2)

The newest tool outputs are usually the largest context consumers and the most expendable — and every one of them is already persisted. `pub/func/message/overflow_clip.py::clip_overflow_tail` turns that into the **first, zero-LLM move on every overflow path**:

- **Before the routes.** `Summarization._dispatch_overflow_route` (T1/T2/T3) runs the clip for every non-`fits` route before dispatching; `_forced_recovery_request` (T4/T5) runs it before the forced compression step. When the clip ALONE drops the estimate below the line, the request returns with the stubbed list and the route never executes — no budget truncation, no auxiliary-LLM call.
- **How it clips.** It walks the **trailing contiguous batch of `ToolMessage`s** (stopping at the first non-`ToolMessage` or already-stubbed message) and replaces each content with a compact stub via `model_copy`. Nothing is removed, reordered, or injected: `id`, `tool_call_id`, `name`, and `additional_kwargs` survive, so tool-call/result pairing and the persistence watermark stay intact.
- **Markers survive.** A stubbed message re-emits the P0-2 `[evicted to: …]` pointer (plus a `read_file` hint) and the P2-4 slice notice verbatim, so recovery paths keep working after a clip.
- **Idempotent.** A stub ends the scannable batch, so a second pass is a no-op — the T4/T5 retry budget cannot be burned on identical clips, and the next attempt degrades to compression.
- **Bounded.** `overflow_clip_max_remove` (10) caps how many messages one clip may stub; `overflow_clip_min_keep` (5) disables clipping on short transcripts; `overflow_clip_enabled` is the master switch.

### Zero-LLM Fast Path vs. Degrade Path

| Situation | What runs | LLM calls |
|---|---|---|
| Clip alone drops the estimate below the threshold | stubbed list returned; route never executes | **0** |
| Clip insufficient (or disabled / no eligible batch) | clip result discarded; the existing route runs on the **exact original list** | route-dependent (compact routes call the auxiliary LLM) |
| T4/T5 provider error, first recovery attempt | clip first; if sufficient, retry the provider call with the stubbed list | **0** |
| T4/T5 provider error, clip insufficient | forced compact + budget truncation, then retry | 1 auxiliary-LLM call per compact step (≤ `MAX_OVERFLOW_RETRIES = 3`) |

Acceptance is strict: the clip is applied **only** when it alone brings the pure-local estimate under the line (`estimate_messages_tokens(messages, reported_tokens=0)` — a stale `usage_metadata` never drives recovery). An insufficient clip is discarded so the existing route operates on the original list.

## 🧵 Chain-Summary Filtering

A compression replaces history with a `HumanMessage` / `AIMessage` pair tagged `lc_source="summarization"` (the AI half carries the summary inside `<summary>` tags). On the next compression, feeding that old summary back into the serialized `<conversation>` would waste tokens and confuse the summarizer.

`agent/middlewares/summarization/core.py` handles this in two steps:

1. `_extract_previous_summary` pulls the previous summary text out of the transcript (searching for the newest tagged `AIMessage`, then a tagged `HumanMessage`).
2. `_filter_summary_messages` removes **every** `lc_source="summarization"` message from the list that gets serialized into the new prompt. `_build_summary_prompt` injects the extracted text separately as `<prior-summary>`, next to the update instructions; the model is told to construct one combined summary and that the prior summary is discarded after this.

The pair itself also never reaches MesMemory (see [Persistence](#-persistence-at-every-boundary)) — raw history stays raw.

## 🔗 Interactions & Ordering

Ordering guarantees (verified in `agent/core.py`, list order = registration order):

| Hook phase | Order that matters here |
|---|---|
| `before_agent` (list order) | `MultimodalProcessor` runs **before** the model loop, so P1-9 tagging always sees the final text with media hints already merged |
| `before_model` (list order) | P1-9 tag runs before `ToolCallNormalize` / `SubagentCompletionDrainMiddleware` |
| `wrap_model_call` (outermost → innermost) | ContextEviction (P1-9 view replacement) → … → Summarization (innermost, closest to the LLM) |
| `after_model` (reverse order) | `MessagePersistenceMiddleware` is the **first** hook after the model — the AI message is persisted before HITL strips denied tool calls or raises `GraphInterrupt` |
| `wrap_tool_call` (outermost → innermost) | IterationBudget → ToolGuardrails → ContextEviction → PathGuard → HeartbeatStaleness → HumanInTheLoop → **MessagePersistence (innermost)** — the raw result is flushed first, the preview is swapped in on the way out |

The interaction map:

| Interacting mechanism | What happens |
|---|---|
| **Summarization / compaction** | P1-9 keeps the full human text in state, so compression still sees it; summary pairs are filtered from persistence and from the next `<conversation>` |
| **P1-2 overflow tail clip** | Only stubs `ToolMessage` contents via `model_copy`; it never touches `HumanMessage`s, so the `lc_evicted_to` tag and the full state text survive every clip; stubs carry the P0-2 / P2-4 markers forward |
| **Chain-summary filtering** | Removes `lc_source="summarization"` messages from the serialized conversation only; the state transcript and the MesMemory store are untouched |
| **HITL** | Denials bypass the tool-return flush (HITL wraps persistence from outside) and persist at the next boundary; the persistence batch re-attaches denied tool calls so the denial keeps a paired AI row. HITL never sees `HumanMessage`s, so eviction is orthogonal |
| **`message_search`** | FTS5/SQLite search runs over MesMemory, where the **full** tool result and the **full** human text are archived — eviction only shrinks the model view |
| **Prefix caching** | P1-9 updates the message in place by id (same content, same id) and rewrites nothing else, so the model-visible prefix is only invalidated when the preview view actually differs; tool eviction happens before the message ever enters state, so the preview is the only version the model ever sees |
| **Tool pairing / sanitizer** | All replacements preserve `id` and `tool_call_id`; `sanitize_tool_use_result_pairing` never needs to repair an eviction, and the stubs remain valid pairing inputs |
| **Subagent sessions** | Child pipelines do **not** register `ContextEvictionMiddleware` or `MessagePersistenceMiddleware`: child transcripts keep their full tool results and stay checkpoint-only |

## 🛠️ Configuration

`TOOL_RESULT_EVICTION` (`config/features/agent_side/tool_result_eviction.py`):

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `True` | Master switch for tool-result eviction (P0-2) |
| `evict_threshold_chars` | `20_000` | Text above this many chars is evicted |
| `preview_head_lines` / `preview_tail_lines` | `5` / `5` | Preview head/tail line counts |
| `eviction_subdir` | `"evicted"` | Subdirectory under `SESSIONS_DIR/<session_id>/` |
| `excluded_tools` | 8 names | Never evicted (`read_file` takes the slice path instead) |
| `human_evict_enabled` | `True` | Master switch for human-message eviction (P1-9) |
| `human_evict_threshold_chars` | `200_000` | Human-message trigger threshold |
| `human_preview_head_lines` / `human_preview_tail_lines` | `5` / `5` | Human preview head/tail line counts |

`SUMMARIZATION` (`config/features/agent_side/summarization.py`), the keys this page relies on:

| Key | Default | Meaning |
|---|---|---|
| `overflow_clip_enabled` | `True` | P1-2 tail-clip master switch |
| `overflow_clip_max_remove` | `10` | Max trailing messages stubbed by one clip |
| `overflow_clip_min_keep` | `5` | Transcript floor: at/below this length, never clip |
| `max_tool_output_chars` | `2_000` | Compression-time clip budget for a tool result |
| `content_head_ratio` / `content_tail_ratio` | `0.3` / `0.3` | Head/tail keep ratios for compression-time clips |

There are no environment variables for these knobs: they are code defaults in the feature TypedDicts above, by design.

## 🧪 Testing Map

| Suite | Covers |
|---|---|
| `tests/agent/middlewares/context_eviction/test_context_eviction.py` | P0-2/P2-4 middleware behavior: eviction, exclusion, slice, watermark cover, fail-open |
| `tests/agent/middlewares/context_eviction/test_human_eviction.py` | P1-9 tagging, in-place reducer update, model-view truncation, self-heal, media preservation |
| `tests/agent/middlewares/message_persistence/test_message_persistence.py` | Boundary persistence, watermark write-once, denial re-pairing |
| `tests/agent/middlewares/message_persistence/test_tool_result_persistence.py` | Tool-return flush and its id-less fingerprint / marker interaction |
| `tests/agent/middlewares/message_persistence/test_compression_no_persistence.py` | The compression path writes nothing to MesMemory |
| `tests/pub/func/message/test_eviction.py` | Pure eviction primitives: thresholds, previews, idempotency, unsafe session ids |
| `tests/pub/func/message/test_read_file_slice.py` | P2-4 execution-time slice and its idempotency |
| `tests/pub/func/message/test_overflow_clip.py` | P1-2 pure clip: trailing-batch detection, gates, token target, marker preservation |
| `tests/agent/middlewares/test_summarization_overflow_clip.py` | P1-2 middleware integration: zero-LLM recovery, degradation, T4/T5, sync/async parity |
| `tests/agent/middlewares/test_summary_message_filtering.py` | Chain-summary filtering and `<prior-summary>` injection |
| `tests/context_engine/store/test_persisted_message_ids.py` | The `persisted_message_ids` watermark store |
| `tests/full/test_context_governance_e2e.py` | Live-network e2e (real LLM + real graph): all six mechanisms end to end — run explicitly |

```bash
# Hermetic suites (CI gate)
uv run pytest tests/agent/middlewares/context_eviction \
    tests/agent/middlewares/message_persistence \
    tests/pub/func/message/test_eviction.py \
    tests/pub/func/message/test_read_file_slice.py \
    tests/pub/func/message/test_overflow_clip.py \
    tests/agent/middlewares/test_summarization_overflow_clip.py \
    tests/agent/middlewares/test_summary_message_filtering.py \
    tests/context_engine/store/test_persisted_message_ids.py -q

# Live-network e2e — RUN EXPLICITLY, never part of the CI gate
uv run --no-sync pytest tests/full/test_context_governance_e2e.py -v
```

## 🧹 Clearing Semantics & Limitations

- **`clear_session` removes everything at once.** `server/DAO/messages.py::clear_session` deletes the session's MesMemory rows (which drops the `persisted_message_ids` watermark with them), the checkpointer history, and the whole `SESSIONS_DIR/<session_id>/` folder — so **`evicted/` files and `plans/` are deleted together with the session** (`config/path.py::session_plans_dir` documents the same wholesale contract). The in-memory registers are cleared last.
- **Eviction is a model-view reduction, never a deletion.** Every payload this page shrinks is either archived in MesMemory, present in full in graph state (human messages), or on disk under `evicted/` — and every preview carries a pointer to it.
- **The `evicted/` directory is session-owned but not garbage-collected.** Files live until `clear_session`; there is no per-message TTL. Long sessions with many huge tool results can accumulate disk usage under `workspace/sessions/<session_id>/evicted/`.
- **A single giant line is never evicted.** When the head and tail would both contain the whole payload, the preview cannot be smaller than the original, and the message is left untouched (tool path and human path both).
- **`read_file` slices are not recoverable from the slice itself — by design.** The source file is the recovery path; the notice tells the model exactly how to continue. Only a missing/deleted file defeats it.
- **Human eviction is limited to the trailing message.** A huge payload followed by another user turn is not re-examined; the gate is deliberately "last message only" to avoid revisiting settled history.
- **The tail clip only helps when the trailing batch is large.** A transcript whose context is consumed by human turns or by non-tool messages degrades to the existing routes; P1-2 is an optimization, not a guarantee.
- **Subagent transcripts are out of scope.** Children keep full tool results (no eviction, no persistence) — their transcripts are checkpoint-only and never enter the client-visible MesMemory history.
