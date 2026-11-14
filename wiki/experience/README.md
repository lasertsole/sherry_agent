# Experience System Architecture

English · [中文](README.zh.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

This document covers the experience system: **when** the agent extracts experience, **by which mechanism**, **where** that experience is written, and how the resulting skill library is maintained. Four extraction paths are wired into the agent lifecycle: the compression-time memory review (on every compression, handed the messages about to be discarded and run cache-warm on the turn's own model + prefix), the **skill review** when the plan completes at a turn boundary, the post-compression todo fork, and the MAIN-session deletion salvage (a detached two-wave memory review over the captured compression cut). The outputs land in four stores — MEMORY.md / USER.md / FACTS.md, `skills/auto/`, and `registry.db` — plus the raw `handoff-tree.json` audit file; the **Curator** section below documents the background pass that maintains the `skills/auto/` store that the skill review writes into.

> **Storage update (2026-11-04):** the three memory outputs named below as MEMORY.md / USER.md / FACTS.md are now rows in the MesMemory `memory_entries` table, one row per target (`memory` / `user` / `facts`). The legacy Markdown files are imported once at boot by `memory_migration.py` and renamed to `*.md.bak`. `MemoryStore.load_from_store()` is the loader; each row carries `origin_session` (provenance) and `usage_count` / `last_used` (usage feedback).

> **Module layout (2026-11-13):** the background-review jobs live in dedicated siblings under `agent/middlewares/summarization/`: `memory_review.py`, `session_end_salvage.py`, `handoff_tree.py`, `skill_review_runner.py`, `ptc_compile.py`, `compression_todo.py`, plus the shared `background_scaffold.py` / `_common.py`; `skill_review.py` remains the gate. `background_reviews.py` is a re-export facade whose module proxy forwards attribute writes to the implementation modules, so the historical symbols and monkeypatch seams are unchanged.

> Every claim below was verified against the source. Symbol names, config keys, defaults and paths all exist in code in `agent/middlewares/`, `agent/tools/`, and `config/features/`.

## Design Principles

1. **Every extraction extends an existing store.** Activity lands in MEMORY.md / USER.md / FACTS.md, `skills/auto/`, or `registry.db`; the raw handoff tree is an audit file, not a model-facing knowledge store. No extraction path creates a parallel store.
2. **Fail-open everywhere.** Every trigger logs its failure and swallows it. A broken todo store, an unreadable plan file, or a failed LLM call never blocks or breaks the main conversation turn.
3. **Zero blocking on the turn path.** The post-compression todo fork and the compression-time reviews are fire-and-forget background tasks; the memory review and skill review run as detached sub-agents under the BACKGROUND_REVIEW lane and never block the model call.
4. **Every extraction path needs tools.** Memory review, skill review and the todo fork are all `create_agent` forks because each writes through a tool (`memory`, `skill_manage`, `todowrite`); there is no standalone extraction call left.
5. **Skill-only plan completion.** The plan-completion pass has no knowledge write and no FACTS write. Its durable output is a class-level skill change, or a judge-approved no-op.

## Trigger × Mechanism × Destination

| Trigger | Mechanism (fork agent? call shape?) | Destination |
|---|---|---|
| Every compression | Memory review (`_execute_memory_review`): the DISCARDED slice, sent VERBATIM on the turn's own model configuration (`_build_session_model`) with the session's system prompt and tool face, so the request is a prefix-cache hit — `BACKGROUND_REVIEW_CONFIG["memory_review_mode"] = "digest"` folds it instead (recent 24 messages verbatim, older ones one truncated line each) | MEMORY.md / USER.md / FACTS.md via the `memory` tool |
| The plan completes — every todo `completed` / `cancelled` at a turn boundary (NOT at compression) | Skill review (`_run_skill_review`): the completion hook (`PlanContinuationEnforcer` → `maybe_trigger_skill_review`) forks a dedicated agent on the AUXILIARY model with `_SKILL_REVIEW_PROMPT` and NO conversation history | `skills/auto/` (class-level skills + `<module>-notes`); a judge-approved no-op is allowed; the raw `handoff-tree.json` is written first for audit |
| Post-compression (a cut actually discards messages) | Todo fork (`update_todos_from_compaction`): fire-and-forget background-review agent with `_COMPRESSION_TODO_PROMPT` | `registry.db` via a main-session-bound `todowrite` shim |
| Deleting a MAIN session (before purge) | Session-end salvage (`salvage_session_memory`): snapshots the checkpoint transcript, captures the model/system prompt/tool face, computes the compression cutoff READ-ONLY, then runs two detached memory reviews — wave 1 over the would-be-compressed prefix, wave 2 over the preserved tail after reloading memory and rebuilding the prompt | MEMORY.md / USER.md / FACTS.md via the `memory` tool |

## Trigger Details

### 1. Compression-time memory review

`schedule_memory_review` (`agent/middlewares/summarization/background_reviews.py`, called by the Summarization middleware whenever a compact discards messages) dispatches `_execute_memory_review(session_id, system_prompt, messages_to_summarize)` as a fire-and-forget task on every compression, running under the `background_review_memory_lock` (`state_register_mem`). The messages handed over are the DISCARDED slice — those are the facts about to be lost, and they are a fraction of the conversation — and in the default `cache_warm` mode they go to the model VERBATIM (`BACKGROUND_REVIEW_CONFIG["memory_review_mode"] = "digest"` folds them with `digest_history` first). While either review lock is held, the compression skips dispatch entirely (nothing is queued).

`_execute_memory_review` (`agent/middlewares/summarization/background_reviews.py`) builds the review agent via `_create_memory_review_agent` and invokes it with the discarded slice plus the rendered `_MEMORY_REVIEW_PROMPT` appended as a `HumanMessage`. **Cache-warm by construction:** the compaction cut is a PREFIX cut, so the discarded slice is exactly the part of the request that sits right after the system prompt — sending it back with the same model configuration (`_build_session_model`: the session's provider/model/thinking/temperature, rebuilt as a fresh instance because the graph's clients are closed every turn), the same system prompt and the same tool face reproduces the main model's own prefix, which the provider then serves from its prefix cache; only the review instruction is fresh input and the review still reads the REAL discarded text. The guards are unchanged: the discarded slice instead of the whole conversation (never the preserved remainder), and the pass never blocks the turn (fire-and-forget under the BACKGROUND_REVIEW lane) — the review runs exactly when the context is at its fullest. The prompt asks the agent to save durable user traits (persona, preferences, personal details) and behavioral expectations under the `user` target, and SYSTEM-LEVEL / CROSS-PROJECT pitfalls under the `facts` target (project-specific facts go to MEMORY.md or AGENTS.md instead). It is rendered with the current FACTS.md content and char limit (`_render_prompt_facts`) and tells the agent to maintain the file like an editor — merge entries describing the same pitfall, replace outdated ones, prefer saving nothing over bloating the file. Otherwise it answers "Nothing to save." and stops.

- The review agent is a separate `create_agent` on the SESSION's own model (`_build_session_model`, for prefix-cache reuse) with middleware `[_BackgroundReviewLimitTool(), ToolCallNormalize(), ToolGuardrails(), IterationBudget(90)]` and no checkpointer.
- `_BackgroundReviewLimitTool` (no `allowed_metadata_key`) admits only tools whose metadata carries `background_review: True`. `memory` carries that marker (plus the interactive `question` and the two plan-mode tools), so the memory review can write memory but cannot call arbitrary main tools; the retired `knowledge` tool is no longer part of the face. The skills toolset (`skill_list` / `skill_view` / `skill_manage`) deliberately does **not**: skill maintenance belongs to the plan-completion pass, which binds its own explicit tool face instead of riding the tag.
- The fork's messages are logged only. Nothing from `res["messages"]` reaches the main graph.

### 2. Skill review (todo-complete, at the turn boundary)

`_detect_todo_all_complete(session_id)` (`summarization/background_reviews.py`) is evaluated at every turn whose plan may be complete and fires once per completion cycle:

- todos exist and every todo is `completed` or `cancelled`;
- `skill_review_fired` (`state_register_db`) is not already set.

It sets the fired flag on the transition and resets it to `False` whenever the list is not all-complete, so a later all-complete cycle fires again. Reads are fail-open. Detection runs on EVERY turn whose plan is complete (the completion hook), so a short session extracts too.

The flag alone cannot see an in-place edit of a completed todo, so the trigger also compares a plan **snapshot fingerprint** (`_plan_snapshot_hash`: id + content + status + plan_ref) against the one stored under `skill_review_snapshot_hash` after the last CONSUMED pass (a successful extraction, or a plan the generalizability pre-check judged non-generalizable) — changed content re-extracts, an unchanged plan is skipped, and a failed pass retries after the failure cooldown.

When `skill_review_enabled` is on, `_run_skill_review` is dispatched fire-and-forget by the hook and runs under `skill_review_lock` on the BACKGROUND_REVIEW lane:

1. `_build_plan_context` gathers the plan file (`plan_ref` state first, else the first todo carrying one), the todo list, this session's subagent runs (`result_text` truncated to 24 KB, `outcome`, task), and a bounded `feedback` block: the `skill_view` calls found in recent messages plus recent usage-sidecar entries (`loaded_skills`), and user-authored corrections from the recent turns (`user_corrections`). Internal carriers, system reminders and workspace notices are excluded. It returns `{}` when there is no todo list, which makes the caller skip the pass.
2. Before the LLM, the raw merged handoff tree is written atomically to `handoff-tree.json` for audit. It is NOT a model-facing knowledge read path.
3. The prompt `_SKILL_REVIEW_PROMPT` is rendered with the compact context and sent to a dedicated agent (`_create_skill_review_agent`: the AUXILIARY model at `temperature=0`) — with **no conversation at all**. Its tool face is bound explicitly to `skill_list` / `skill_view` / `skill_manage`; `knowledge` and `memory` are absent, so this pass cannot write knowledge or FACTS.
4. The prompt maps evidence explicitly: `success_steps` -> procedure / verification steps, `failed_branches` -> pitfalls / `when_not_to_use`, `hard_constraints` -> guardrails, `locked_decisions` -> rationale, `user_corrections` -> body / `when_to_use`. It prefers a loaded skill, then an existing class-level umbrella, then a support file, and only then a new class-level skill. A no-op is allowed only with a concrete reason. On visibility it OMITS `scope` by default (visible to the main agent and subagents), writes `main_only` only when the procedure depends on main-agent-only capabilities (delegation orchestration such as `sessions_spawn` / `taskflow_*` / `todowrite`, `skill_manage`), and `subagent_only` only for a pure worker playbook — and the quality judge returns `scope_valid`, failing the attempt with feedback when the declared scope contradicts the body.

Before the LLM work, a **task-level pre-check** (`_judge_task_generalizability`) screens the plan once: one short auxiliary call receives only the plan goal and the completed todo summaries (never the handoff evidence) and answers `{"should_extract": bool, "reason": str}`. A `false` verdict returns before the handoff tree and the review agent are built and stores the snapshot fingerprint as consumed — the same `skill_review_snapshot_hash` the success path writes — so the same plan never re-runs the pre-check. A malformed, unavailable or failed pre-check fails OPEN to extraction: the full review plus its quality judge still own skill-content quality, and the pre-check only decides whether the TASK class is worth reviewing at all.

The review runs as a snapshot/judge transaction:

- **snapshot**: `skill_review.snapshot_auto_skills()` captures `skills/auto` (skills, support files and the usage sidecar) before the first attempt.
- **write**: the review creates/patches skills. `set_current_write_origin("background_review")` marks newly created skills as agent-created for the curator.
- **judge**: `skill_review.judge_skill_update()` asks the auxiliary model for `verdict`, `grounded_in_run`, `reusable`, `trigger_quality`, `quality`, `no_op_justified`, `scope_valid` and `feedback`. A change passes only with `quality >= BACKGROUND_REVIEW_CONFIG["skill_review_quality_threshold"]`, `trigger_quality >= 6`, grounded and reusable; a no-op needs `no_op_justified`.
- **retry/rollback**: a rejected attempt restores the pre-review snapshot, then retries with the judge feedback (default max 2 attempts). Exhaustion restores again, records `skill_review_failed_hash` + timestamp, and does not advance the success fingerprint; a later turn can retry after `skill_review_failure_cooldown_s`, and a changed plan retries immediately.
- **optional PTC compile (after a pass)**: when `PTC["ptc_prefill_enabled"]` is on, the same background task compiles one `scripts/ptc__<plan_key>__<step_id>.py` per successful TaskFlow step into the updated skill. Compilation is best-effort and never reruns the judge or rolls back the accepted skill.

Knowledge files (`task-*.json` / `wave-*.json` / `plan-summary.json`) and the FACTS target are no longer written by this path. Existing knowledge files remain on disk but are legacy; the model-facing prompt block and the `knowledge` tool are retired.

### 3. Post-compression todo fork

At the same compression point, `_schedule_compression_todo_update` (`summarization/core.py` -> `background_reviews.schedule_compression_todo_update`) schedules `update_todos_from_compaction` as a fire-and-forget `asyncio.create_task`. The scheduler gates on ALL of:

- `compression_todo_update_enabled` (`SUMMARIZATION`, default `True`);
- the cut actually discarded messages;
- no `compression_todo_update_lock` is already held for the session;
- the session has a non-empty todo list;
- an event loop is running (the sync compression path skips, logging at debug level).

`update_todos_from_compaction` runs a background-review agent with system prompt `_COMPRESSION_TODO_PROMPT` and exactly one tool: `_build_main_session_todowrite(session_id)`. The fork reconciles the current todo list against the discarded slice: it marks actually-finished items `completed`, abandoned or superseded items `cancelled`, adds evidenced new work as `pending`, and writes the COMPLETE list back in one `todowrite` call (full replacement, not a delta). If nothing changed it writes the list back unchanged.

The fork result messages are logged only. Nothing reaches the main graph or its checkpointer, and the derived session's middleware state is cleared in `finally`.

### 4. Session deletion (MAIN only)

Before `purge_session` destroys a main session, `salvage_session_memory` snapshots the post-compaction checkpoint transcript plus the model, system prompt and tool face, computes the normal compression cutoff **read-only**, and schedules a detached two-wave review. The delete request does not wait for the model: the snapshot is the only synchronous work, and it has a 5 s budget. Side chats and subagent children never enter this path.

- **Wave 1** covers `messages[:cutoff]` — the prefix a compression would have discarded — with the captured model, system prompt and tool face, so it follows the same cache-warm shape as the compression-time review. No summary is generated and no compression state is touched.
- **Wave 2** covers `messages[cutoff:]` — the preserved tail. After wave 1 writes memory, the task reloads `MEMORY.md`/`USER.md`/`FACTS.md`, rebuilds the system prompt without persisting it (the session is gone), and runs the tail with a `memory`-only tool face. A rebuild failure falls back to the captured prompt.
- Both waves run under the BACKGROUND_REVIEW lane with per-wave derived session keys (`<session_id>::session-end-salvage:1|2`) and no checkpointer; the keys are cleared in `finally`. The `memory` tool's `background_review_memory` metadata is the execution gate, so the first wave can mirror the session tool face for cache-warm without being able to call `knowledge` or plan tools after the purge.
- Every failure — snapshot, prompt rebuild, timeout, model error — is logged and swallowed. A process exit can abandon an unfinished background review; this is the accepted trade-off for immediate deletion. Config: `BACKGROUND_REVIEW_CONFIG["session_end_review_enabled"]` and `BACKGROUND_REVIEW_CONFIG["session_end_review_timeout_s"]` (per wave, default 120 s).

## Curator (Skill Curation)

**Frontmatter contract (the index IS the trigger).** `<available_skills>` renders only a skill's frontmatter — the body is read after loading — so those fields are the routing signal: `description` (capability summary), `when_to_use` (**required** on every skill these paths create or patch: the specific scenarios / user intents that should load it) and `when_not_to_use` (optional: exclusions and alternatives). An optional `roles: [...]` list narrows a skill to specific subagent functional roles (omitting it keeps the skill visible to every role; the main agent and the identity GENERAL role skip the check). The loader renders both extra tags with CJK-aware token budgets (60 / 50 / 50 tokens) and XML-escapes them; the client's skill tooltip shows the same three fields. The curator adds a second, narrower level: the umbrella's frontmatter covers the whole skill, and every `##` sub-capability section opens with its own `**When to use:**` line (plus `**When NOT to use:**` when there is a genuine exclusion), so the model can pick the right section after `skill_view`. Its deterministic fallback writes the same shape, extracting the conditions from the merge reasons.

The Curator (`context_engine/curator/`) is the background maintenance pass that owns the lifecycle of the skill library under `skills/auto/` — exactly the store extraction path 2 above writes into. It only reads and writes skills; MEMORY.md / USER.md / FACTS.md and `registry.db` are outside its scope.

**What it is.** An inactivity-triggered orchestrator, not a scheduled cron. The service entry point starts a daemon thread (`curator-timer`) through `context_engine.curator.init()` (`server/__main__.py:140`; skipped in HTTP-only mode, `server/__main__.py:66-73`). The thread wakes every 3600 s and calls `maybe_run_curator(idle_for_seconds=...)` (`context_engine/curator/__init__.py:122-140`). Importing the package is side-effect-free — only `init()` starts the thread (`context_engine/curator/__init__.py:151-165`).

**When it runs.** A review executes only when both gates pass:

- `should_run_now()` — enabled, not paused, and `last_run_at` older than the effective interval (`context_engine/curator/transitions.py:21-39`). The effective interval is `curator.interval_hours`, default 168 h / 7 days (`config/sherry_settings.py:43`), overridable from the client to 1–5 days via `auto_interval_days` in `.curator_state` (`context_engine/curator/config.py:97-107`);
- the agent has been idle for at least `curator.min_idle_hours`, default 2 (`context_engine/curator/orchestrator.py:108-115`). Every user turn resets that idle timer (`server/service/messages.py:190`).

The UI can force a run through `POST /curator/run`, which calls `run_curator_review()` in a worker thread (`server/trigger/http/curator.py:129-160`). The archive is exposed beside it: `POST /curator/restore` (body `{"name": "<skill>"}` → `{success, message}`; HTTP 400 for unknown, bundled/hub-shadowed, or occupied names) and `GET /curator/archived` (`{success, archived, count}`), both backed by the curator-layer `restore_skill()` / `list_archived()` (`server/trigger/http/curator.py:163-204`).

**Lifecycle rules.** `apply_automatic_transitions()` (`context_engine/curator/transitions.py:41-91`) walks every `skills/auto/**/SKILL.md` (`context_engine/curator/usage.py:161-180`) and, for each skill: skips pinned skills; marks it `stale` after `stale_after_days` without activity (default 30); past `archive_after_days` (default 90) it is **archived** into `skills/.archive/` and marked `archived`; reactivates skills that show activity again, and never-used skills still inside the stale window. **All three curator removals are recoverable**: the 90-day transition, consolidation sources (archived with an `ABSORBED_INTO` marker naming their umbrella), and pruned skills all move into `skills/.archive/`, and `curator restore <name>` brings any of them back. Restoring flips the curator-side usage record back to `active` in the same step (the agent-side forwarder also mirrors the agent telemetry record), so a restored skill immediately re-enters `apply_automatic_transitions` instead of hitting none of its four branches; same-second archive collisions get distinct sibling directories (`<skill>-<timestamp>`), never a nested move. Only an explicit delete — `skill_manage(action="delete")` or the client's delete endpoint — removes a skill irreversibly. The archive is not auto-purged — restore entries or clean it up manually. Defaults live in `config/sherry_settings.py:42-48`.

**LLM consolidation.** When `curator.consolidate` is on (default true), `run_curator_review()` renders the candidate list of non-pinned skills (`context_engine/curator/review.py:69`) and asks the main LLM at temperature 0.3 to merge overlapping narrow skills into class-level umbrella skills (`CURATOR_REVIEW_PROMPT`, `context_engine/curator/review.py:16`). New umbrellas and their support files are generated and persisted under `skills/auto/` (`_generate_umbrella_skill`, `context_engine/curator/umbrella.py:86`; `_apply_consolidation`, `context_engine/curator/orchestrator.py:163`). Consolidation is the only LLM step of the pass; failures are caught and the run continues.

**Relation to the four extraction paths.** Path 2 (the skill review) is the producer: it creates or patches `skills/auto/` through `skill_manage`. The Curator is the downstream maintainer of that same output — it transitions, consolidates and prunes what the review created. The other three paths never touch `skills/auto/` (they write MEMORY.md / USER.md / FACTS.md, `registry.db`, or the deletion-time memory salvage), so they never intersect the Curator.

**State and boundaries.** Run state lives in `skills/.curator_state` (`context_engine/curator/constants.py:3`, loaded/saved by `context_engine/curator/state.py`); each run writes `run.json` + `REPORT.md` under `logs/curator/{timestamp}/` (`context_engine/curator/constants.py:4`; `context_engine/curator/report.py:196-205`). Scope is `skills/auto/` only — built-in skills are never touched, pinned skills bypass every destructive transition, and the whole pass runs in a background thread, off the conversation turn path. Full detail: [curator/README.md](../../context_engine/curator/README.md).

## Isolation & Safety

| Guard | What it protects |
|---|---|
| Background-review metadata allowlist (`metadata["background_review"]`, `_BackgroundReviewLimitTool` default) | The memory review may only call tools tagged for a background review. Every other main tool returns an error `ToolMessage` instead of executing. |
| Skill-review tool face | The plan-completion skill review binds only `skill_list` / `skill_view` / `skill_manage`; knowledge and memory are absent by construction. |
| Skill-review rollback | Every attempt snapshots `skills/auto`; a rejected judge verdict restores that snapshot before the retry. |
| Compression metadata allowlist (`metadata["todo_update"] is True`, `_BackgroundReviewLimitTool(allowed_metadata_key="todo_update")`) | The compression fork admits exactly the metadata-marked `todowrite` shim. |
| Derived session key `<id>::compression-todo` | The compression fork's `IterationBudget` / `ToolGuardrails` / `ToolCallNormalize` state keys cannot collide with the main session, which is mid `awrap_model_call` while the fork runs. Only `compression_todo_update_lock` is deliberately written on the main session, as the cross-path re-entrancy coordinator. |
| Derived session keys `<id>::session-end-salvage:1|2` | The deletion waves run after the session's registers are cleared; their engine state lives under per-wave keys and is cleared in `finally`, so the review cannot resurrect the deleted session's state. |
| `background_review_memory` execution gate (`metadata["background_review_memory"] is True`, `_BackgroundReviewLimitTool(allowed_metadata_key="background_review_memory")`) | Session-end wave 1 can mirror the session tool face for cache-warm while only the `memory` tool is executable; knowledge and plan tools are retired after deletion. |
| Main-session-bound `todowrite` shim | The fork graph runs under the derived key, so the state-injected real `todowrite` would resolve the wrong session. The shim reuses the real tool's `args_schema` and `description` verbatim (zero schema drift), drops the injected `session_id`, and binds the main session id captured at build time. |
| Read-only forks, no checkpointer | Every background-review / skill-review fork's result messages are logged and discarded. The forks have no checkpointer, so they cannot write the main graph's state. |
| Per-session re-entrancy locks | `background_review_memory_lock`, `skill_review_lock` and `compression_todo_update_lock` prevent overlapping runs of the same extraction path. While a review lock is held, the compression scheduler skips dispatch entirely (nothing is queued). |
| Fail-open boundaries | Every path wraps its work in `try/except`, logs, and returns. No extraction failure propagates into a turn, a compression, or another extraction. |
| Background-task reference retention | The `_COMPRESSION_TODO_TASKS` and `_SESSION_END_REVIEW_TASKS` sets hold strong refs so asyncio cannot garbage-collect an in-flight task. |

## Storage & Limits

| Store | Path (constant) | Limit |
|---|---|---|
| `memory` target | MesMemory `memory_entries` row (`target='memory'`) | 2200 chars (`MEMORY_TOOL["memory_char_limit"]`, overridable via `/memory/settings`) |
| `user` target | MesMemory `memory_entries` row (`target='user'`) | 1375 chars (`MEMORY_TOOL["user_char_limit"]`, overridable) |
| `facts` target | MesMemory `memory_entries` rows (`target='facts'`, one global target) | 1375 chars (`MEMORY_TOOL["facts_char_limit"]`, overridable); an over-limit `add` archives the LEAST-ACTIVE entries |
| legacy plan knowledge | `workspace/knowledge/plans/<plan_key>/` | Retired: existing `task-<n>.json` / `wave-<n>.json` / `plan-summary.json` files remain on disk but are not written or read by the model-facing paths |
| handoff audit | `workspace/knowledge/plans/<plan_key>/handoff-tree.json` | Raw code-merged handoff tree, written before the review; not a prompt block |
| skills | `config.path.AUTO_SKILLS_DIR` (`skills/auto/`) | `SKILL.md` plus `references/`, `templates/`, `scripts/` support files; PTC/LDW artifacts live under `scripts/` as `ptc__<plan_key>__<step_id>.py` / `ldw__<plan_key>.py` |
| todos | `src/data/registry.db` (`todos` table) (`store_sqlite._DB_PATH`) | session-scoped list, full-replacement writes |

## Configuration

| Knob | Where | Default | Effect |
|---|---|---|---|
| `compression_todo_update_enabled` | `SUMMARIZATION` (`config/features/agent_side/summarization.py`) | `True` | Enables the post-compression todo fork |
| `skill_review_enabled` | `BACKGROUND_REVIEW_CONFIG` (`config/features/agent_side/background_review.py`) | `True` | Master switch for the plan-completion skill review (skill-only; no knowledge writes) |
| `skill_review_max_attempts` | `BACKGROUND_REVIEW_CONFIG` | `2` | Initial attempt + at most one judge-feedback retry |
| `skill_review_quality_threshold` | `BACKGROUND_REVIEW_CONFIG` | `6` | Minimum `quality` score for a pass |
| `skill_review_feedback_turns` / `_max_items` / `_chars` | `BACKGROUND_REVIEW_CONFIG` | `12` / `8` / `2000` | Bounded user-correction context |
| `skill_review_loaded_skills_max` | `BACKGROUND_REVIEW_CONFIG` | `20` | Cap on the loaded-skill list (transcript + usage fallback) |
| `skill_review_failure_cooldown_s` | `BACKGROUND_REVIEW_CONFIG` | `3600` | Cooldown before retrying the same failed plan snapshot |
| `session_end_review_enabled` | `BACKGROUND_REVIEW_CONFIG` | `True` | Enables the MAIN-session deletion salvage |
| `session_end_review_timeout_s` | `BACKGROUND_REVIEW_CONFIG` | `120` | Per-wave model-call budget for the deletion salvage |
| `compaction_cooldown_rounds` | `SUMMARIZATION` | `3` | Proactive-compression cooldown after an actual compression |
| `memory_char_limit` / `user_char_limit` / `facts_char_limit` | `MEMORY_TOOL` (`config/features/agent_side/memory_tool.py`) | `2200` / `1375` / `1375` | MEMORY.md / USER.md / FACTS.md caps |

## Verification

Focused tests:

```bash
uv run pytest \
    tests/agent/middlewares/test_session_end_review.py \
    tests/agent/middlewares/test_compression_todo_update.py \
    tests/agent/middlewares/test_compression_background_reviews.py \
    tests/agent/middlewares/test_compression_cooldown_persist.py \
    tests/agent/middlewares/system_prompt/test_skill_review.py \
    tests/agent/middlewares/test_skill_review_module_notes.py \
    tests/agent/middlewares/test_skill_review_gate.py \
    tests/agent/middlewares/test_skill_review_feedback.py \
    tests/agent/middlewares/test_skill_review.py \
    tests/agent/tools/test_memory_store.py \
    tests/agent/tools/test_memory_store_facts.py -q
```

- `test_session_end_review.py`: the deletion snapshot, the read-only compression split, wave 1 → prompt rebuild → wave 2 ordering, cutoff/empty/switch/duplicate/failure branches, derived-state cleanup, and both waves reaching MEMORY.md/USER.md.
- `test_compression_todo_update.py`: trigger gating, fire-and-forget scheduling, re-entrancy lock, fail-open release, prompt content, the `todo_update` metadata gate, and full-fork isolation (derived key, main-session `todowrite` shim, no checkpointer / message leakage).
- `test_compression_background_reviews.py`: compression-time dispatch (the memory review fires from the compact seam; a no-cut compaction dispatches nothing). `test_memory_review_digest.py` pins the discarded-slice wiring, the cache-warm model rebuild (descriptor fields, env-default fallback, session tool face) and the fold under ``memory_review_mode="digest"``. Persistence assertions live in `tests/agent/middlewares/message_persistence/`.
- `test_compression_cooldown_persist.py`: cooldown survival across restarts.
- `test_skill_review_trigger.py`: the `_detect_todo_all_complete` branches, the per-compression memory-review dispatch and lock semantics of `schedule_memory_review`, and `_build_plan_context`.
- `test_skill_review_module_notes.py`: the `<module>-notes` carry-forward guidance in the skill-only prompt, plus the stub-LLM behavior (create lands `skills/auto/<module>-notes/`; an existing skill is patched, never duplicated).
- `test_skill_review_gate.py`: snapshot/restore rollback, changed-path detection, judge pass/retry/no-op/unavailable verdicts.
- `test_skill_review_feedback.py`: bounded user corrections, internal-carrier filtering, `skill_view` loaded-skill extraction and usage fallback.
- `test_skill_review.py`: approved no-op, judge-feedback retry, snapshot rollback, exhausted-attempt failure marker and failure cooldown.
- `test_memory_store.py`: MEMORY.md / USER.md store semantics.
- `test_memory_store_facts.py`: the `facts` target — add/replace/remove, oldest-first rollover, atomic writes, load-time file creation, live content rendering, and prompt-snapshot injection (empty file → no block).

AI-judged evaluation: `evals/skill_review/suite.py` seeds a completed-plan run (plan file, completed todos, synthetic subagent runs), invokes the real `_run_skill_review`, then asks an auxiliary LLM judge whether the produced skill is genuinely grounded in that run, reusable, and non-generic. Every write is redirected into the sandbox, so the real `skills/auto/` and `workspace/` are never touched.

```bash
uv run python evals/evals.py skill_review
```

The suite checks `knowledge_retired`, `skills_created`, `ai_judge_skill_review`, and `no_repo_pollution`.

## Known Boundaries

- **The compression fork is not given `todoread`.** Only the `todo_update`-marked `todowrite` shim is admitted; the fork receives the current list in its prompt, and `todoread` is intentionally left untagged (`agent/tools/todolist/tools/__init__.py`).
- **FACTS.md is a single-file memory.** It shares the MEMORY.md / USER.md machinery (one file, `§`-delimited entries, atomic rename writes, per-file char cap) and is injected into every system prompt; it is not a tiered or classified store, and the oldest entries roll off when the cap is exceeded.
- **The cooldown suppresses proactive compression only.** `compaction_cooldown_rounds` (3) blocks T1 / T2 / T3 compaction after an actual compression; the T4 / T5 provider-error recovery bypasses the cooldown, the per-turn attempt cap, and the other anti-thrash gates by construction.

## Related Documentation

- [Session Memory Architecture](../session_memory/README.md): the session memory architecture that hosts these stores.
- [Summarization](../summarization/README.md): compression triggers and the cooldown that gates the background reviews and todo fork.
- [Long-Running Tasks](../long-running-tasks/README.md): TaskFlow and the todo planning layer that the skill review reads from.
- [Middlewares README](../../agent/middlewares/README.md): `@dynamic_prompt` system-prompt injection and Summarization reference.
- [Curator README](../../context_engine/curator/README.md): the background skill-maintenance orchestrator documented above.
