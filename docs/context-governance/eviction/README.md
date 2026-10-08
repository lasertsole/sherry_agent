# 🗜️ Eviction, Media & Overflow

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> Part of [Context Governance](../README.md): the persistence floor at every boundary, the three eviction paths, the `read_file` slice, media governance, the overflow tail clip, and chain-summary filtering.

---

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

## 🖼️ Media Governance (Offload, Reference & Token Classing)

Media is governed on the way in and on the way out: entry-side rules keep unprocessable payloads out of the request and stop an unusable model from being probed twice, while compression and token estimation never treat base64 as text.

**Input size cap.** Every inbound media payload — an inline base64 / `data:` block or a remote URL download — is measured against `MEDIA_PIPELINE["max_media_bytes"]` (20 MiB) **before any disk write**. A payload over the cap is skipped: nothing is written, no path enters `MediaPaths`, a warning logs the byte count, and the message carries a model-visible `[Uploaded media]` line saying the attachment was not saved and was not sent to the model. A remote URL is judged by `Content-Length` when the server declares one and otherwise by a read capped at `limit + 1` bytes, so a wrong header can never force an oversized write; the same gate covers the image, audio and video handlers (`media_handlers.py::_exceeds_media_limit` / `_record_oversize`). A payload exactly at the limit is allowed.

**Per-request capability scrub.** `MultimodalProcessor.wrap_model_call` (auto mode) rebuilds the request copy only — `request.override(messages=...)` — replacing every media block whose family the serving model is cached as `"unsupported"` with a text placeholder that names the block type, its recorded on-disk path and the matching builtin skill (`image_to_text` / `speech_to_text` / `video_text_to_text`). Supported and unprobed blocks pass through verbatim, so a mixed message keeps native media for the supported families and only the unsupported ones are stripped — block by block. State, checkpointer and MesMemory are never written; when no block needs replacement the original request object is returned. The scrub runs only under `"auto"`: `"true"` keeps every block for the model, `"false"` takes the skill path before the request is assembled.

**Silent-degradation detection.** A model can accept media blocks and answer as if it never saw them. On the success path of a call that started as an auto-mode native attempt, `LLMRetryMiddleware` evaluates the reply with `detect_media_blindness()` (`media_pipeline/degradation.py`) — a pure regex, en / zh / ja / ko, precision-first: a blindness phrase counts only when a media word sits within ±40 characters — plus an explicit "please describe the media" request pattern. On a hit every media family present in the request is cached `"unsupported"`, so later turns go straight to the skill path; the per-turn native flag is cleared either way. `main_llm_silent_degradation_detection` (True) is the master switch. Attribution follows the serving model: when `LLMRetryMiddleware` re-binds the request to a sticky fallback candidate it first rewrites the per-turn native-model key to `{candidate.provider}/{candidate.model_name}`, so an error rejection and a silent one are both cached against the model that actually served the call.

**Capability cache.** All three entry-side behaviors share one process-level cache (`agent/middlewares/llm_capability_cache.py`), keyed `"{provider}/{model_name}"` with per-family values `"auto"` (untested) / `"supported"` / `"unsupported"`. It lives for the process only: a restart starts clean (at most one wasted native probe), and a model switch — env change plus restart — is naturally a new key.

**Compression-time offload.** Before `_apply_compression` serializes the summarized prefix (`current_messages[:cutoff]`), `offload_inline_media` (`agent/middlewares/summarization/media_offload.py`) rewrites every inline media block in that range only:

- a `data:` URL / base64 payload (`image_url` / `audio_url` / `video_url`, a bare `base64` field, `audio_bytes` / `video_bytes`, or an Anthropic-style `source.data`) is decoded and written to `SESSIONS_DIR/<session_id>/media/{sha256(raw)[:16]}{ext}`; the extension comes from the magic bytes via `media_handlers._infer_extension`;
- identical bytes are written once — the content-hash filename deduplicates within and across compactions;
- the block becomes the text pointer `[evicted to: <path>]`, the same marker the P0-2 / P1-9 eviction paths emit, so `_collect_evicted_refs` collects it and `SummaryDoc.evicted_refs` carries the path onto the summary chain (rendered as *Evicted References*);
- a block that cannot be decoded or written becomes `<media error="failed_to_offload" />` — fail-open, never a crash.

The preserved tail window keeps its media. Both compression paths call the offload on the prefix slice — `_apply_compression_under_lock` (sync) and `_aapply_compression_under_lock` (async). The summary prompt carries the media-reference rules: preserve the pointers verbatim, do not invent visual / audio / video details, retrieve the payload from the path. Media files live under the session tree, so `clear_session()` removes them with `evicted/` and `plans/`.

**Token classing.** `pub/func/estimate_tokens.py` counts a content **list** block by block: text blocks as text, media blocks at a fixed per-type cost from `TOKEN_ESTIMATION` (`tokens_per_image_block = 85` — aligned with `langchain_core.count_tokens_approximately`; `tokens_per_audio_block = 256`; `tokens_per_video_block = 1024`; `tokens_per_unknown_block = 85` for unrecognised blocks, including ones hiding a `data:` payload). Base64 is never serialized and never counted as text: the removed JSON path read 5 MB of base64 as ~1.25M tokens, which would fire compression on a single image. `str` content, `None`, and pure-text lists keep their text estimates.

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
