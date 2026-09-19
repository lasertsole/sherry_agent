# Experience System Architecture

English · [中文](README.zh.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

This document covers the experience system: **when** the agent extracts experience, **by which mechanism**, **where** that experience is written, and how the resulting skill library is maintained. Four extraction paths are wired into the agent lifecycle: the compression-time memory review (every `nudge_memory_threshold` compressions), plan extraction when the todo list is all-complete at a compression, the pre-compression memory flush, and the post-compression todo fork. The outputs land in four stores — MEMORY.md / USER.md, the plan knowledge directory, `skills/auto/`, and `todos.db` — and the **Curator** section below documents the background pass that maintains the `skills/auto/` store that plan extraction writes into.

> Every claim below was verified against the source. Symbol names, config keys, defaults and paths all exist in code in `agent/middlewares/`, `agent/tools/`, and `config/features/`.

## Design Principles

1. **Every extraction extends an existing store.** Activity lands in MEMORY.md / USER.md, the plan knowledge directory, `skills/auto/`, or `todos.db`. No extraction path creates a parallel store.
2. **Fail-open everywhere.** Every trigger logs its failure and swallows it. A broken todo store, an unreadable plan file, or a failed LLM call never blocks or breaks the main conversation turn.
3. **Zero blocking on the turn path.** The post-compression todo fork and the compression-time nudges are fire-and-forget background tasks; the memory review and plan extraction run as detached sub-agents under the NUDGE lane and never block the model call.
4. **Right-sized mechanism.** Full `create_agent` forks are reserved for work that needs tool use (memory nudge, plan extraction, todo fork). Pure extraction (memory flush) uses a single auxiliary-LLM call.

## Trigger × Mechanism × Destination

| Trigger | Mechanism (fork agent? call shape?) | Destination |
|---|---|---|
| Every `nudge_memory_threshold` compressions (default 10) | Memory nudge (`_nudge_memory`): forks a `create_agent` nudge agent with `_MEMORY_REVIEW_PROMPT` | MEMORY.md / USER.md via the `memory` tool |
| Todo list all-complete at a compression (`completed` / `cancelled`) | Plan extraction (`_nudge_plan_extraction`): forks a nudge agent with `_PLAN_EXTRACTION_PROMPT` | ① knowledge JSON, ② `skills/auto/` |
| Pre-compression (a cut actually discards messages) | Memory flush (`run_memory_flush[_sync]`): one cheap LLM call, not an agent | MEMORY.md and USER.md via `MemoryStore.append_entries` |
| Post-compression (a cut actually discards messages) | Todo fork (`update_todos_from_compaction`): fire-and-forget nudge agent with `_COMPRESSION_TODO_PROMPT` | `todos.db` via a main-session-bound `todowrite` shim |

## Trigger Details

### 1. Compression-time memory review

`schedule_compression_nudges` (`agent/middlewares/summarization/nudges.py`, called by the Summarization middleware whenever a compact discards messages) increments `nudge_review_memory_count` in `state_register_db` once per compression. When the counter reaches `nudge_memory_threshold` (default 10), it resets the counter to 0 and dispatches `_nudge_memory(session_id, system_prompt, messages)` as a fire-and-forget task that runs under the `nudge_review_memory_lock` (`state_register_mem`). While either nudge lock is held, the compression still increments the counter but no dispatch happens.

`_nudge_memory` (`agent/middlewares/summarization/nudges.py`) builds a nudge agent via `_create_nudge_agent` and invokes it with the conversation plus `_MEMORY_REVIEW_PROMPT` appended as a `HumanMessage`. The prompt asks the agent to save durable user traits (persona, preferences, personal details) and behavioral expectations, using the `memory` tool; otherwise it answers "Nothing to save." and stops.

- The nudge agent is a separate `create_agent` on the main LLM with middleware `[_NudgeLimitTool(), ToolCallNormalize(), ToolGuardrails(), IterationBudget(90)]` and no checkpointer.
- `_NudgeLimitTool` (no `allowed_metadata_key`) admits only tools whose metadata carries `nudge: True`. `memory`, `skill_list`, `skill_view`, `skill_manage` and `knowledge` carry that marker, so the nudge agent can write memory but cannot call arbitrary main tools.
- The fork's messages are logged only. Nothing from `res["messages"]` reaches the main graph.

### 2. Compression-time plan extraction (todo-complete)

`_detect_todo_all_complete(session_id)` (`summarization/nudges.py`) is evaluated at every compression and fires once per completion cycle:

- todos exist and every todo is `completed` or `cancelled`;
- `nudge_plan_extraction_fired` (`state_register_db`) is not already set.

It sets the fired flag on the transition and resets it to `False` whenever the list is not all-complete, so a later all-complete cycle fires again. Reads are fail-open. Because detection runs only at compression time, a session that never compresses never fires plan extraction.

When `plan_extraction_enabled` is on and detection fires, `_nudge_plan_extraction` is dispatched fire-and-forget from the same seam and runs under `nudge_plan_extraction_lock`:

1. `_build_plan_context` gathers the plan file (`plan_ref` state first, else the first todo carrying one), the todo list, the start-work ledger (`.omo/start-work/ledger.jsonl`), and this session's subagent runs (`result_text` truncated to 24 KB, `outcome`, task). It returns `{}` when there is no todo list, which makes the caller skip the pass.
2. The prompt `_PLAN_EXTRACTION_PROMPT` is rendered with the plan context, then sent to a nudge agent (same builder, same `nudge: True` gate) with the conversation.

The prompt produces two outputs:

- **Part 1: structured knowledge.** `knowledge(action="write", ...)` writes JSON documents under `config.path.PLAN_KNOWLEDGE_DIR` (`workspace/knowledge/plans/<plan_key>/`, keyed by plan identity — see `agent/tools/todolist/knowledge/identity.py`): `task-<position>.json`, `wave-<index>.json`, `plan-summary.json`. Each task carries `failure_set`, `success_path`, `method`; waves carry failure / success patterns; the plan carries overall method, key failures / successes, and reusable patterns.
- **Part 2: skill library update.** `skill_manage` patches a loaded or existing class-level skill, adds a support file, or creates a new class-level umbrella under `skills/auto/`. The prompt is explicitly active ("most completed plans produce at least one skill update") and lists user corrections, workflow corrections, non-trivial techniques, and outdated skills as first-class signals.

Reads are served later by the same tool (`knowledge(action="read")`), and a condensed plan summary is auto-injected into the system prompt by `build_knowledge_block` (`knowledge/prompt_block.py`).

### 3. Pre-compression memory flush

Both compression paths (`_apply_compression_under_lock` and `_aapply_compression_under_lock` in `agent/middlewares/summarization/core.py`) run the flush when a cutoff discards messages, before the summary is generated:

- `run_memory_flush_sync(...)` on the sync path;
- `await run_memory_flush(...)` on the async path.

`should_flush` (`agent/middlewares/summarization/memory_flush.py`) gates on `MEMORY_FLUSH["enabled"]` plus either `total_chars >= force_flush_chars` (50 000) or `estimated_tokens >= soft_threshold_tokens` (8 000). The flush is one `llm.ainvoke` / `llm.invoke` of `_FLUSH_PROMPT` over the about-to-be-discarded text, built by `_build_llm` with `model=MEMORY_FLUSH["model"]`, `max_tokens=2048`, `timeout=30`. It is **not** an agent and has no tools.

The response is parsed for `§`-delimited entries. An empty result or `(none)` writes nothing. Otherwise `MemoryStore.append_entries` routes each entry: entries matching `^\s*user\s*:` (case-insensitive) go to USER.md, everything else (Environment / Project / Decision / Tool / no prefix) goes to MEMORY.md. `append_entries` deduplicates against the target file and, unlike `add`, evicts the oldest entries to stay inside each file's own limit.

The flush only extracts cross-session facts. Temporary task progress is deliberately left to the summary. Any exception is logged and swallowed; the flush never blocks compression.

### 4. Post-compression todo fork

At the same compression point, `_schedule_compression_todo_update` (`summarization/core.py` -> `nudges.schedule_compression_todo_update`) schedules `update_todos_from_compaction` as a fire-and-forget `asyncio.create_task`. The scheduler gates on ALL of:

- `compression_todo_update_enabled` (`SUMMARIZATION`, default `True`);
- the cut actually discarded messages;
- no `compression_todo_update_lock` is already held for the session;
- the session has a non-empty todo list;
- an event loop is running (the sync compression path skips, logging at debug level).

`update_todos_from_compaction` runs a nudge agent with system prompt `_COMPRESSION_TODO_PROMPT` and exactly one tool: `_build_main_session_todowrite(session_id)`. The fork reconciles the current todo list against the discarded slice: it marks actually-finished items `completed`, abandoned or superseded items `cancelled`, adds evidenced new work as `pending`, and writes the COMPLETE list back in one `todowrite` call (full replacement, not a delta). If nothing changed it writes the list back unchanged.

The fork result messages are logged only. Nothing reaches the main graph or its checkpointer, and the derived session's middleware state is cleared in `finally`.

## Curator (Skill Curation)

The Curator (`context_engine/curator/`) is the background maintenance pass that owns the lifecycle of the skill library under `skills/auto/` — exactly the store extraction path 2 above writes into. It only reads and writes skills; MEMORY.md / USER.md, the plan knowledge directory and `todos.db` are outside its scope.

**What it is.** An inactivity-triggered orchestrator, not a scheduled cron. The service entry point starts a daemon thread (`curator-timer`) through `context_engine.curator.init()` (`server/__main__.py:140`; skipped in HTTP-only mode, `server/__main__.py:66-73`). The thread wakes every 3600 s and calls `maybe_run_curator(idle_for_seconds=...)` (`context_engine/curator/__init__.py:122-140`). Importing the package is side-effect-free — only `init()` starts the thread (`context_engine/curator/__init__.py:151-165`).

**When it runs.** A review executes only when both gates pass:

- `should_run_now()` — enabled, not paused, and `last_run_at` older than the effective interval (`context_engine/curator/transitions.py:21-39`). The effective interval is `curator.interval_hours`, default 168 h / 7 days (`config/sherry_settings.py:43`), overridable from the client to 1–5 days via `auto_interval_days` in `.curator_state` (`context_engine/curator/config.py:97-107`);
- the agent has been idle for at least `curator.min_idle_hours`, default 2 (`context_engine/curator/orchestrator.py:320-336`). Every user turn resets that idle timer (`server/service/messages.py:190`).

The UI can force a run through `POST /curator/run`, which calls `run_curator_review()` in a worker thread (`server/trigger/http/curator.py:129-160`). The archive is exposed beside it: `POST /curator/restore` (body `{"name": "<skill>"}` → `{success, message}`; HTTP 400 for unknown, bundled/hub-shadowed, or occupied names) and `GET /curator/archived` (`{success, archived, count}`), both backed by the curator-layer `restore_skill()` / `list_archived()` (`server/trigger/http/curator.py:163-204`).

**Lifecycle rules.** `apply_automatic_transitions()` (`context_engine/curator/transitions.py:41-100`) walks every `skills/auto/**/SKILL.md` (`context_engine/curator/usage.py:158-179`) and, for each skill: skips pinned skills; marks it `stale` after `stale_after_days` without activity (default 30); past `archive_after_days` (default 90) it is **archived** into `skills/.archive/` and marked `archived`; reactivates skills that show activity again, and never-used skills still inside the stale window. **All three curator removals are recoverable**: the 90-day transition, consolidation sources (archived with an `ABSORBED_INTO` marker naming their umbrella), and pruned skills all move into `skills/.archive/`, and `curator restore <name>` brings any of them back. Restoring flips the curator-side usage record back to `active` in the same step (the agent-side forwarder also mirrors the agent telemetry record), so a restored skill immediately re-enters `apply_automatic_transitions` instead of hitting none of its four branches; same-second archive collisions get distinct sibling directories (`<skill>-<timestamp>`), never a nested move. Only an explicit delete — `skill_manage(action="delete")` or the client's delete endpoint — removes a skill irreversibly. The archive is not auto-purged — restore entries or clean it up manually. Defaults live in `config/sherry_settings.py:42-48`.

**LLM consolidation.** When `curator.consolidate` is on (default true), `run_curator_review()` renders the candidate list of non-pinned skills (`context_engine/curator/orchestrator.py:65-81`) and asks the main LLM at temperature 0.3 to merge overlapping narrow skills into class-level umbrella skills (`CURATOR_REVIEW_PROMPT`, `context_engine/curator/orchestrator.py:18-45`). New umbrellas and their support files are generated and persisted under `skills/auto/` (`_generate_umbrella_skill`, `context_engine/curator/orchestrator.py:412`; `_apply_consolidation`, `context_engine/curator/orchestrator.py:755`). Consolidation is the only LLM step of the pass; failures are caught and the run continues.

**Relation to the four extraction paths.** Path 2 (compression-time plan extraction) is the producer: it creates or patches `skills/auto/` through `skill_manage`. The Curator is the downstream maintainer of that same output — it transitions, consolidates and prunes what plan extraction created. The other three paths never touch `skills/auto/` (they write MEMORY.md / USER.md, the plan knowledge directory, and `todos.db`), so they never intersect the Curator.

**State and boundaries.** Run state lives in `skills/.curator_state` (`context_engine/curator/constants.py:3`, loaded/saved by `context_engine/curator/state.py`); each run writes `run.json` + `REPORT.md` under `logs/curator/{timestamp}/` (`context_engine/curator/constants.py:4`; `context_engine/curator/report.py:196-205`). Scope is `skills/auto/` only — built-in skills are never touched, pinned skills bypass every destructive transition, and the whole pass runs in a background thread, off the conversation turn path. Full detail: [curator/README.md](../../context_engine/curator/README.md).

## Isolation & Safety

| Guard | What it protects |
|---|---|
| Nudge metadata allowlist (`metadata["nudge"]`, `_NudgeLimitTool` default) | Memory nudge and plan extraction may only call tools tagged for the nudge phase. Every other main tool returns an error `ToolMessage` instead of executing. |
| Compression metadata allowlist (`metadata["todo_update"] is True`, `_NudgeLimitTool(allowed_metadata_key="todo_update")`) | The compression fork admits exactly the metadata-marked `todowrite` shim. |
| Derived session key `<id>::compression-todo` | The compression fork's `IterationBudget` / `ToolGuardrails` / `ToolCallNormalize` state keys cannot collide with the main session, which is mid `awrap_model_call` while the fork runs. Only `compression_todo_update_lock` is deliberately written on the main session, as the cross-path re-entrancy coordinator. |
| Main-session-bound `todowrite` shim | The fork graph runs under the derived key, so the state-injected real `todowrite` would resolve the wrong session. The shim reuses the real tool's `args_schema` and `description` verbatim (zero schema drift), drops the injected `session_id`, and binds the main session id captured at build time. |
| Read-only forks, no checkpointer | Every nudge / extraction fork's result messages are logged and discarded. The forks have no checkpointer, so they cannot write the main graph's state. |
| Per-session re-entrancy locks | `nudge_review_memory_lock`, `nudge_plan_extraction_lock` and `compression_todo_update_lock` prevent overlapping runs of the same extraction path. While a nudge lock is held, the compression scheduler counts the compression but skips dispatch. |
| Fail-open boundaries | Every path wraps its work in `try/except`, logs, and returns. No extraction failure propagates into a turn, a compression, or another extraction. |
| Background-task reference retention | The `_COMPRESSION_TODO_TASKS` set holds strong refs so asyncio cannot garbage-collect an in-flight task. |

## Storage & Limits

| Store | Path (constant) | Limit |
|---|---|---|
| MEMORY.md | `config.path.MEMORY_DIR / "MEMORY.md"` (`workspace/memory/MEMORY.md`) | 2200 chars (`MemoryStore.memory_char_limit`) |
| USER.md | `config.path.MEMORY_DIR / "USER.md"` (`workspace/memory/USER.md`) | 1375 chars (`MemoryStore.user_char_limit`) |
| plan knowledge | `config.path.PLAN_KNOWLEDGE_DIR` (`workspace/knowledge/plans/<plan_key>/` — identity-keyed) | `task-<n>.json`, `wave-<n>.json`, `plan-summary.json` per plan; prompt-guided field caps (150 / 100 chars) |
| skills | `config.path.AUTO_SKILLS_DIR` (`skills/auto/`) | `SKILL.md` plus `references/`, `templates/`, `scripts/` support files |
| todos | `agent/tools/todolist/data/todos.db` (`store_sqlite._DB_PATH`) | session-scoped list, full-replacement writes |

## Configuration

| Knob | Where | Default | Effect |
|---|---|---|---|
| `MEMORY_FLUSH_ENABLED` | env -> `MEMORY_FLUSH["enabled"]` (`config/features/agent_side/memory_flush.py`) | `1` (on) | Master switch for the pre-compression flush |
| `MEMORY_FLUSH_MODEL` | env -> `MEMORY_FLUSH["model"]` | `""` (use the factory default) | Cheap extraction model for the flush |
| `soft_threshold_tokens` | `MEMORY_FLUSH` | `8000` | Flush when the discarded slice is at least this large |
| `force_flush_chars` | `MEMORY_FLUSH` | `50000` | Flush unconditionally above this character count |
| `output_max_tokens` | `MEMORY_FLUSH` | `2048` | Flush call output cap |
| `timeout_seconds` | `MEMORY_FLUSH` | `30` | Flush call timeout |
| `compression_todo_update_enabled` | `SUMMARIZATION` (`config/features/agent_side/summarization.py`) | `True` | Enables the post-compression todo fork |
| `plan_extraction_enabled` | `NUDGE` (`config/features/agent_side/nudge.py`) | `True` | Enables todo-complete plan extraction |
| `nudge_memory_threshold` | `NUDGE` | `10` | Compressions between memory reviews |
| `compaction_cooldown_rounds` | `SUMMARIZATION` | `3` | Proactive-compression cooldown after an actual compression |
| `memory_char_limit` / `user_char_limit` | `MemoryStore.__init__` | `2200` / `1375` | MEMORY.md / USER.md caps |

## Verification

Focused tests:

```bash
uv run pytest \
    tests/agent/middlewares/test_compression_todo_update.py \
    tests/agent/middlewares/test_compression_nudges.py \
    tests/agent/middlewares/test_compression_cooldown_persist.py \
    tests/agent/middlewares/test_memory_flush.py \
    tests/agent/middlewares/system_prompt/test_plan_extraction.py \
    tests/agent/tools/test_memory_store.py -q
```

- `test_compression_todo_update.py`: trigger gating, fire-and-forget scheduling, re-entrancy lock, fail-open release, prompt content, the `todo_update` metadata gate, and full-fork isolation (derived key, main-session `todowrite` shim, no checkpointer / message leakage).
- `test_compression_nudges.py`: compression-time nudge dispatch (memory review + plan extraction fire from the compact seam; a no-cut compaction dispatches nothing). Persistence assertions live in `tests/agent/middlewares/message_persistence/`.
- `test_compression_cooldown_persist.py`: cooldown survival across restarts.
- `test_memory_flush.py`: flush gating, routing, and non-blocking failure.
- `test_plan_extraction.py`: the four `_detect_todo_all_complete` branches, the compression-time counter/lock semantics of `schedule_compression_nudges`, dispatch, and `_build_plan_context`.
- `test_memory_store.py`: MEMORY.md / USER.md store semantics.

AI-judged evaluation: `evals/nudge_extraction/suite.py` seeds a completed-plan run (plan file, completed todos, synthetic subagent runs), invokes the real `_nudge_plan_extraction`, then asks an auxiliary LLM judge whether the produced skill is genuinely grounded in that run, reusable, and non-generic. Every write is redirected into the sandbox, so the real `skills/auto/` and `workspace/` are never touched.

```bash
uv run python evals/evals.py nudge_extraction
```

The suite checks `knowledge_written`, `skills_created`, `ai_judge_skill_quality`, and `no_repo_pollution`.

## Known Boundaries

- **The compression fork is not given `todoread`.** Only the `todo_update`-marked `todowrite` shim is admitted; the fork receives the current list in its prompt, and `todoread` is intentionally left untagged (`agent/tools/todolist/tools/__init__.py`).
- **Memory flush extracts cross-session facts only.** Temporary task progress belongs to the summary, not to MEMORY.md / USER.md.
- **The cooldown suppresses proactive compression only.** `compaction_cooldown_rounds` (3) blocks T1 / T2 / T3 compaction after an actual compression; the T4 / T5 provider-error recovery bypasses the cooldown, the per-turn attempt cap, and the other anti-thrash gates by construction.

## Related Documentation

- [Session Memory Architecture](../session_memory/README.md): the pre-compression memory flush.
- [Summarization](../summarization/README.md): compression triggers and the cooldown that gates the flush and todo fork.
- [Long-Running Tasks](../long-running-tasks/README.md): TaskFlow and the todo planning layer that plan extraction reads from.
- [Middlewares README](../../agent/middlewares/README.md): `@dynamic_prompt` system-prompt injection and Summarization reference.
- [Curator README](../../context_engine/curator/README.md): the background skill-maintenance orchestrator documented above.
