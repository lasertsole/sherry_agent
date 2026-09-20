# 🧭 Context Governance: Persistence, Eviction, Slices & Overflow Clip

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> How raw history is kept durable and the model-visible context is kept small: write-once persistence at every model boundary and tool return, tool-result eviction to disk with a recoverable preview, execution-time `read_file` slicing, human-message eviction with a full-text state, media governance (an input size cap, per-request capability scrubbing, silent-degradation detection, compression-time offload, and block-wise token classing), a no-LLM overflow tail clip that runs before any compression route, and chain-summary filtering that keeps old summaries out of the conversation payload.

Every message the agent produces is valuable twice: once as **raw history** (what actually happened, for search and for compression) and once as **model context** (what fits in the window right now). This page documents the mechanisms that reconcile those two needs — they all share one rule: **once a payload has entered the pipeline it is never lost, only the model view is shrunk, and every reduction leaves a pointer back to the full text.**

**Source of truth:** `agent/middlewares/context_eviction/core.py`, `agent/middlewares/message_persistence/core.py`, `agent/middlewares/message_persistence/prepare.py`, `pub/func/message/eviction.py`, `pub/func/message/overflow_clip.py`, `pub/func/message/target_truncation.py`, `pub/func/message/tool_args_truncate.py`, `agent/middlewares/summarization/core.py`, `agent/middlewares/summarization/media_offload.py`, `agent/middlewares/media_pipeline/scrub.py`, `agent/middlewares/media_pipeline/degradation.py`, `agent/middlewares/media_pipeline/media_handlers.py`, `agent/middlewares/llm_capability_cache.py`, `agent/middlewares/llm_retry/core.py`, `pub/func/estimate_tokens.py`, `context_engine/store/core.py`, `config/features/agent_side/tool_result_eviction.py`, `config/features/agent_side/summarization.py`, `config/features/agent_side/media_pipeline.py`, `config/features/agent_side/token_estimation.py`. Every claim below was checked against that code.

## Table of Contents

- [Overview & Pipeline](#-overview--pipeline)
- [Information Sources](#%EF%B8%8F-information-sources)
- [Eviction, Media & Overflow](eviction/README.md)
  - [💾 Persistence at Every Boundary](eviction/README.md#-persistence-at-every-boundary)
  - [🗜️ Tool-Result Eviction (P0-2)](eviction/README.md#%EF%B8%8F-tool-result-eviction-p0-2)
  - [✂️ `read_file` Slice (P2-4)](eviction/README.md#%EF%B8%8F-read_file-slice-p2-4)
  - [📥 Human-Message Eviction (P1-9)](eviction/README.md#-human-message-eviction-p1-9)
  - [🖼️ Media Governance (Offload, Reference & Token Classing)](eviction/README.md#%EF%B8%8F-media-governance-offload-reference--token-classing)
  - [⚡ Overflow Tail Clip (P1-2)](eviction/README.md#-overflow-tail-clip-p1-2)
  - [🧵 Chain-Summary Filtering](eviction/README.md#-chain-summary-filtering)
- [Interactions & Ordering](#-interactions--ordering)
- [Configuration](#%EF%B8%8F-configuration)
- [Testing Map](#-testing-map)
- [Clearing Semantics & Limitations](#-clearing-semantics--limitations)

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
| **Media offload (compression)** | `offload_inline_media` | none | inline media in the summarized prefix → on-disk copy + `[evicted to: …]` pointer; preserved tail untouched |

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
| Media file | non-message — disk file | — | upload processing (`MultimodalProcessor`), native history stripping, or compression-time offload (`offload_inline_media`) | `SESSIONS_DIR/<session_id>/media/` (durable copies; compression-time offload names by `sha256[:16]` and dedups); a payload over `max_media_bytes` is never written | media blocks when native; skill-path hints and per-request scrub placeholders carry the path; the summary chain keeps offloaded paths in `evicted_refs[]` |
| Plan knowledge | non-message — disk directory | — | plan extraction | `workspace/knowledge/plans/<plan_key>/` | injected as a `<knowledge>` block via `plan_ref` |
| FACTS.md | `workspace/memory/FACTS.md` (memory tool target `facts`) | — | broad, module-independent pitfalls and conventions: compression-time memory review + completed-plan extraction | memory file (1 375-char limit; overflow drops the oldest entries first) | injected into every system prompt as a resident FACTS memory block |

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

`TOKEN_ESTIMATION` (`config/features/agent_side/token_estimation.py`), multimodal token classing:

| Key | Default | Meaning |
|---|---|---|
| `chars_per_token` / `chars_per_token_cjk` | `4` / `2` | Text-estimate divisors (non-CJK / CJK) |
| `tokens_per_image_block` | `85` | Fixed cost per image block (aligned with `langchain_core.count_tokens_approximately`) |
| `tokens_per_audio_block` / `tokens_per_video_block` | `256` / `1024` | Conservative fixed costs (no duration metadata at estimation time) |
| `tokens_per_unknown_block` | `85` | Fixed cost for an unrecognised block — never its base64 |

`MEDIA_PIPELINE` (`config/features/agent_side/media_pipeline.py`), the input-side keys:

| Key | Default | Meaning |
|---|---|---|
| `main_llm_native_multimodal` | `"auto"` | Tri-state native switch: `"true"` keeps media blocks for the model, `"false"` always takes the skill path, `"auto"` decides per family through the capability cache; any other value fails safe to the skill path |
| `main_llm_silent_degradation_detection` | `True` | Cache the media families present as `"unsupported"` when a native reply self-reports media blindness |
| `max_media_bytes` | `20 * 1024 * 1024` | Hard per-payload ceiling; an oversize payload is skipped before any disk write |

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
| `tests/agent/middlewares/test_compression_media_offload.py` | Compression-time inline-media offload: write + pointer, hash dedup, failure placeholder, preserved window untouched, sync/async, `evicted_refs` |
| `tests/pub/func/test_estimate_tokens_media.py` | Block-wise multimodal estimation: fixed media costs, 5 MB-base64 regression, unknown-block media, `str` / `None` / empty-list edges |
| `tests/agent/middlewares/test_multimodal_processor.py` | Tri-state native switch and per-request scrub: mixed families strip only unsupported blocks, supported blocks kept verbatim, state/kwargs/disk untouched, `"true"` never scrubbed, history image stripping |
| `tests/agent/middlewares/test_media_size_limit.py` | `max_media_bytes` cap: oversize payloads skipped before any write, model-visible notice, exactly-at-limit allowed, declared `Content-Length` fast path, capped read |
| `tests/agent/middlewares/test_media_degradation.py` | Silent-degradation detection: en / zh / ja / ko blindness regex + describe-request patterns, capable or unrelated replies stay clean, every present family cached, flag cleared either way, fallback-candidate attribution |
| `tests/agent/middlewares/test_multimodal_native_fallback_e2e.py` | Rejection → cache + skill-path rewrite: next-session native skip, per-model-key isolation, explicit `"true"` never falls back |
| `tests/context_engine/store/test_persisted_message_ids.py` | The `persisted_message_ids` watermark store |
| `tests/full/test_context_governance_e2e.py` | Live-network e2e (real LLM + real graph): all mechanisms end to end — run explicitly |

```bash
# Hermetic suites (CI gate)
uv run pytest tests/agent/middlewares/context_eviction \
    tests/agent/middlewares/message_persistence \
    tests/pub/func/message/test_eviction.py \
    tests/pub/func/message/test_read_file_slice.py \
    tests/pub/func/message/test_overflow_clip.py \
    tests/agent/middlewares/test_summarization_overflow_clip.py \
    tests/agent/middlewares/test_summary_message_filtering.py \
    tests/agent/middlewares/test_multimodal_processor.py \
    tests/agent/middlewares/test_media_size_limit.py \
    tests/agent/middlewares/test_media_degradation.py \
    tests/agent/middlewares/test_multimodal_native_fallback_e2e.py \
    tests/agent/middlewares/test_compression_media_offload.py \
    tests/pub/func/test_estimate_tokens_media.py \
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
