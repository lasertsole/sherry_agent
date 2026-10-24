# 🧠 Memory & Continuity

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> Part of [Long-Running Tasks](../README.md): the two-layer memory system, summary ↔ TaskFlow coordination, subagent memory backflow, one-line tool-output summaries, session continuity, and TaskFlow auto-resume.

---

## 🧠 Layered Memory

Two layers, distinguished by *how* they reach the model:

| Layer | Store | Location | In the prompt? |
| :--- | :--- | :--- | :--- |
| **L1 — curated memory** | `MEMORY.md` (agent notes) + `USER.md` (user profile) | `workspace/memory/` (`MEMORY_DIR`) | Yes — a frozen snapshot, always injected |
| **L2 — raw history** | `mes_memory.db` (SQLite, WAL, FTS5) | `src/store/mes_memory/mes_memory.db` | No — retrieved by `context_engine` / `message_search` |

Files are **plain text entries separated by the delimiter `§` on its own line** — `ENTRY_DELIMITER = "\n§\n"` (`agent/tools/memory.py:53`), no YAML frontmatter and no bullet prefixing. Entries may be multiline.

Layer 1 is managed by `MemoryStore` (`memory.py:104`): per-file character limits `2200` (memory) and `1375` (user), an injection scan (`_MEMORY_THREAT_PATTERNS`, `memory.py:68`) that rejects prompt-injection and credential-exfiltration content, cross-platform file locking, atomic writes, and exact-match dedup. The live entries are mutated immediately, while the prompt uses a **frozen snapshot** captured at `load_from_disk()` to keep the prefix cache stable for the session.

The `memory` tool is tagged `scope="main_only"`, so subagents never see it.

**Graph-state checkpoint store.** Sessions also persist their LangGraph state to `src/checkpoints/sqlite.db`, separate from the two layers above: every `built_agent()` call prunes it to the latest checkpoint per thread (`ThreadSafeAsyncSqliteSaver.aclean_old_checkpoints`, `agent/core.py:227`), and because `auto_vacuum=0` that DELETE only frees pages instead of shrinking the file, the same call reads `PRAGMA freelist_count × page_size` right after pruning and runs `VACUUM` only when the freed space exceeds `_VACUUM_THRESHOLD_BYTES` (10 MB, `agent/checkpointer/thread_safe_checkpointer.py`) — fail-open: a VACUUM error is logged and the prune result stands.

## 🔗 Summary ↔ TaskFlow Coordination

When compression builds its LLM prompt, `_get_taskflow_context_sync(session_id)` (`agent/middlewares/summarization/core.py:122`) renders this session's active flows and appends them as the **last** part of the summary prompt (`_build_summary_prompt`, `summarization/summary_generation.py:554`):

```python
taskflow_ctx = _get_taskflow_context_sync(session_id)
if taskflow_ctx:
    parts.append(taskflow_ctx)
```

The block is headed `## Current TaskFlow State (authoritative)` (`summarization/core.py:137`) and, for up to three flows owned by the session (the store read is scoped by `session_id` in SQL — no post-filtering), lists the flow id/status, description, `done/total` progress with the status breakdown, the last two completed steps, the first two pending steps, and any wait reason. It reuses the DAG helpers `step_status` and `steps_summary`, and is fully fail-open (`except Exception → ""`). The deterministic fallback summary (`_build_static_fallback_summary`) does **not** include this block; it is an LLM-prompt-only addition.

## 🧠 Subagent Memory Backflow

`SubagentCompletionDrainMiddleware` (`agent/middlewares/subagent_completion_drain/core.py`) is the parent-turn ingestion point for queued subagent completions: at `before_model` it rehydrates and drains the session's `SteeringQueue` and injects the rebuilt completion-carrier messages. **When the drain is non-empty** it also reconciles the shared memory with the parent's in-memory view:

```python
# subagent_completion_drain/core.py:93-117
def _backflow_shared_memory() -> None:
    from agent.tools.memory import memory_store
    memory_store.load_from_disk()
    for target in ("memory", "user"):
        memory_store.save_to_disk(target)
```

Parent and children share **one process-wide `MemoryStore`**, so a child's writes are already file-visible. What can drift is the parent's in-memory view — the live entries plus the **frozen snapshot** the system prompt was built from — when a writer outside this process updated `MEMORY.md` / `USER.md`. The **reload-first** order is load-bearing: persisting the stale in-memory list before reloading would clobber a concurrent writer, so the reconcile must load → persist per target.

Like the drain, the backflow is **fail-open** — a memory-I/O failure is logged and swallowed, and the completion carrier still reaches the parent turn. The drain keeps the parent honest about completions: when the session has no passing evidence, it appends a mandatory-verification gate message after the carriers, so the parent treats a completion as a `DoneClaim`, not a verified result (verify via `todoread`, check acceptance criteria, and probe for stale state before marking a todo complete).

## ✂️ Tool Output Summarization

Older oversized `ToolMessage` content is normally cleared to a marker during non-LLM pruning. `pub/func/message/tool_output_prune.py` replaces the bare `_PRUNE_MARKER = "[Old tool result content cleared]"` (`tool_output_prune.py:22`) with a **one-line, tool-specific summary** so the model retains a hint of what was in the result:

```python
# _TOOL_SUMMARY_TEMPLATES (tool_output_prune.py:43-65)
"read"/"read_file"   -> "[read] read file, {len} chars, {lines} lines"
"write"/"write_file" -> "[write] wrote file, {len} chars"
"edit"/"edit_file"   -> "[edit] edited file, {len} chars"
"grep"               -> "[grep] search done, {lines} matches"
"glob"               -> "[glob] matched {n} files"
"bash"/"shell"       -> "[bash] exit_code={code}, output {len} chars"
"taskflow_flow"      -> "[taskflow_flow] {len} chars"
"taskflow_step"      -> "[taskflow_step] {len} chars"
"taskflow_inspect"   -> "[taskflow_inspect] {len} chars"
"memory"             -> "[memory] {first 80 chars}..."
default              -> "[tool] output {len} chars, first 100: ..."
```

`prune_tool_outputs(messages, protect_tokens=…, min_reduction_tokens=…, protected_tools=None, estimator=None)` (`tool_output_prune.py:104`) walks messages newest→oldest, stops at the first summary message, protects the newest `prune_protect_tokens` (40 000), skips protected tools (`{"memory", "skill_view", "skill_list"}`), and only commits when the freed tokens reach `prune_min_reduction_tokens` (5 000). Replaced messages are `model_copy` clones carrying `additional_kwargs["status"] = "compacted"` and `["original_length"]`. Summaries are capped at 200 chars; any template exception falls back to the marker. It is called from `Summarization._run_non_llm_strategies` (`summarization/compression.py:314`).

## 🔄 Session Continuity

When a session is cleared, `context_engine/session_continuity.py` persists an end-state so the next session can offer continuity. `server/service/session_cleanup_service.py::purge_session` calls `auto_save_on_session_end(session_id)` as **step 0**, before any deletion (`server/service/session_cleanup_service.py` step 0). That function:

1. Resolves `channel_id`/`chat_id` through `runtime.session.relation_register` (`_get_channel_chat_for_session`, `session_continuity.py:167`).
2. Reads the last 3 turns and clips the last AI reply to `_MAX_SUMMARY_CHARS = 500` (`session_continuity.py:28`).
3. Collects active flow ids for the session.
4. Writes `save_session_end_state(...)` to a JSON file at `src/data/session_continuity/{safe-key}.json` (`session_continuity.py:25`), with fields `last_session_id`, `ended_at`, `ended_ts`, `summary`, `taskflow_ids`.

After the message-store deletion, `clear_session` also purges the session's **planning stores** — `agent.tools.todolist.registry.store_sqlite.delete_todos_by_session(session_id)` and `agent.tools.taskflow.registry.store_sqlite.delete_flows_by_session(session_id)` — so a cleared session leaves no todo or task-flow debris. The deletions are best-effort (a failure is logged and never blocks the rest of the purge), only rows whose `session_id` matches are deleted, and pre-isolation task-flow rows (`session_id = ''`) are never matched.

The next session reads it via `build_continuity_prompt(session_id)` (`session_continuity.py:80`), called by `_build_continuity_block` in `workspace/prompt_builder.py:169` and injected when the full prompt is built (`prompt_builder.py:289-295`):

```
## Last Session (continuity)
Last conversation ended with: <summary ≤ 500 chars>
Related tasks: <up to 3 flow ids>
If the user says 'continue' or doesn't specify a new task, refer to the above context first.
```

A session never receives its own state (`last_session_id == session_id → ""`). Because the lookup requires **both** a channel id and a chat id, pure WebSocket sessions with no channel binding get no continuity block. Storage is filesystem JSON (keyed by `channel:chat`, falling back to `session_id`), not a database.

## ♻️ TaskFlow Auto-Resume

Active flows are re-surfaced into the system prompt so a fresh session can pick up unfinished work. Three independent readers use the same recipe — the session-scoped `get_active_flows_sync(session_id)` reached through `PromptDataProvider.get_active_flows(session_id)`:

| Reader | Location | Purpose |
| :--- | :--- | :--- |
| `_build_taskflow_block` | `workspace/prompt_builder.py:145` | `## Pending TaskFlows` in the system prompt |
| `_get_taskflow_context_sync` | `agent/middlewares/summarization/core.py:122` | TaskFlow block in the compression summary prompt |
| `_get_active_taskflow_ids_sync` | `context_engine/session_continuity.py:215` | `taskflow_ids` in the persisted continuity state |

`creator_session_key` is still stamped on the flow at creation (`taskflow_create.py:38`) as `requester_session_key(session_id)` = `f"agent:main:session:{session_id}"` (`_shared.py:21`) because child dispatch builds the requester key from it; it is no longer the isolation mechanism. `get_active_flows_sync(session_id)` returns only this session's `running` and `waiting` flows ordered by revision, using the stdlib `sqlite3` path that works without an event loop; failures return `[]`.

The system-prompt block (`prompt_builder.py:140`) looks like:

```
## Pending TaskFlows
- [running] flow-1: "<description>" | 2/5 steps done | next: step-3 "<task>"
Use taskflow_inspect(action="summary") to inspect a flow and continue execution.
```

It is capped at three flows and suppressed when the prompt is built with a file filter (`selected_file_names is not None`). Every read is fail-open.

