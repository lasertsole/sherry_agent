# Experience Extraction Architecture

English · [中文](README.zh.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

This document maps **when** the agent extracts experience, **by which mechanism**, and **where** that experience is written. Five extraction paths are wired into the agent lifecycle: the per-turn facts pipeline, the 10-turn memory nudge, plan extraction on todo completion, the pre-compression memory flush, and the post-compression todo fork.

> Every claim below was verified against the source. Symbol names, config keys, defaults and paths all exist in code in `agent/middlewares/`, `context_engine/facts/`, `agent/tools/`, and `config/features/`.

## Design Principles

1. **Every extraction extends an existing store.** Activity lands in MEMORY.md / USER.md, the tiered `facts/*.md` layer, the plan knowledge directory, `skills/auto/`, or `todos.db`. No extraction path creates a parallel store.
2. **Fail-open everywhere.** Every trigger logs its failure and swallows it. A broken todo store, an unreadable plan file, a failed LLM call, or a corrupt cursor never blocks or breaks the main conversation turn.
3. **Zero blocking on the turn path.** The facts pipeline and the post-compression todo fork are fire-and-forget background tasks. The memory nudge and plan extraction run as detached sub-agents (in the async path they run concurrently with persistence via `asyncio.gather`).
4. **Right-sized mechanism.** Full `create_agent` forks are reserved for work that needs tool use (memory nudge, plan extraction, todo fork). Pure extraction (per-turn facts, memory flush) uses a single auxiliary-LLM call.

## Trigger × Mechanism × Destination

| Trigger | Mechanism (fork agent? call shape?) | Destination |
|---|---|---|
| Every turn end | Facts pipeline (`context_engine/facts/`): enqueue the turn, then an auxiliary-LLM extractor. Not an agent. Skipped on a plan-extraction turn, whose own pass absorbs the pending range. | `facts/<category>.md` via `TieredMemoryStore.add_fact` |
| Every 10 turns (`nudge_memory_threshold`) | Memory nudge (`_nudge_memory`): forks a `create_agent` nudge agent with `_MEMORY_REVIEW_PROMPT` | MEMORY.md / USER.md via the `memory` tool |
| Todo list becomes all-complete (`completed` / `cancelled`) | Plan extraction (`_nudge_plan_extraction`): forks a nudge agent with `_PLAN_EXTRACTION_PROMPT` | ① knowledge JSON, ② `skills/auto/`, ③ `facts/*.md` |
| Pre-compression (a cut actually discards messages) | Memory flush (`run_memory_flush[_sync]`): one cheap LLM call, not an agent | MEMORY.md and USER.md via `MemoryStore.append_entries` |
| Post-compression (a cut actually discards messages) | Todo fork (`update_todos_from_compaction`): fire-and-forget nudge agent with `_COMPRESSION_TODO_PROMPT` | `todos.db` via a main-session-bound `todowrite` shim |

## Trigger Details

### 1. Per-turn facts pipeline (session-memory P2-3)

`ContextEngineHook.aafter_agent` (`agent/middlewares/context_engine/core.py`) persists the last turn to MesMemory, then, when `turn_num > 0` and the turn is **not** a plan-extraction turn, creates a background task for `_run_facts_pipeline(session_id, turn_num)`.

`_run_facts_pipeline` calls `enqueue_turn` then `process_pending` (`context_engine/facts/queue.py`):

- `enqueue_turn` advances the `enqueued` watermark (`facts_cursor_enqueued`) to the persisted turn number.
- `process_pending` reads the pending range between the two watermarks, formats the conversation rows, and calls `extract_facts` (`context_engine/facts/extractor.py`) on the auxiliary LLM. Each `{"category", "fact"}` item is written through `TieredMemoryStore.add_fact`, and only then is the `consumed` watermark (`facts_cursor_consumed`) advanced.

The cursor is a **dual watermark** (`context_engine/facts/cursor.py`): both values are monotonic and durable in `state_register_db`, so a crash between enqueue and consume replays the range instead of losing it. This is at-least-once semantics: replayed turns can produce duplicate facts, which `add_fact` deduplicates on exact text.

**Yield on plan-extraction turns.** When plan extraction fires, the per-turn pipeline is not started. `_nudge_plan_extraction` covers the pending range in the same LLM pass (Part 3 below) and advances the consumed watermark only after a successful pass, so the same turn never runs two extractors and no interval is lost.

### 2. Every-10-turn memory nudge

`ContextEngineHook._after_agent_impl` increments `nudge_review_memory_count` in `state_register_db` on every turn. When the counter reaches `nudge_memory_threshold` (default 10), it resets the counter to 0 and `_nudge_memory(session_id, system_prompt, messages)` runs under the `nudge_review_memory_lock` (`state_register_mem`). While either nudge lock is held, `after_agent` skips the nudge decision (the counter still increments).

`_nudge_memory` (`agent/middlewares/context_engine/nudge.py`) builds a nudge agent via `_create_nudge_agent` and invokes it with the conversation plus `_MEMORY_REVIEW_PROMPT` appended as a `HumanMessage`. The prompt asks the agent to save durable user traits (persona, preferences, personal details) and behavioral expectations, using the `memory` tool; otherwise it answers "Nothing to save." and stops.

- The nudge agent is a separate `create_agent` on the main LLM with middleware `[_NudgeLimitTool(), ToolCallNormalize(), ToolGuardrails(), IterationBudget(90)]` and no checkpointer.
- `_NudgeLimitTool` (no `allowed_metadata_key`) admits only tools whose metadata carries `nudge: True`. `memory`, `skill_list`, `skill_view`, `skill_manage` and `knowledge` carry that marker, so the nudge agent can write memory but cannot call arbitrary main tools.
- The fork's messages are logged only. Nothing from `res["messages"]` reaches the main graph.

### 3. Todo-complete plan extraction

`_detect_todo_all_complete(session_id)` (`core.py`) fires once per completion cycle:

- todos exist and every todo is `completed` or `cancelled`;
- `nudge_plan_extraction_fired` (`state_register_db`) is not already set.

It sets the fired flag on the transition and resets it to `False` whenever the list is not (or no longer) all-complete, so a later all-complete cycle fires again. Reads are fail-open.

When `plan_extraction_enabled` is on and detection fires, `_nudge_plan_extraction` runs under `nudge_plan_extraction_lock`:

1. `_build_plan_context` gathers the plan file (`plan_ref` state first, else the first todo carrying one), the todo list, the start-work ledger (`.omo/start-work/ledger.jsonl`), and this session's subagent runs (`result_text` truncated to 24 KB, `outcome`, task). It returns `{}` when there is no todo list, and returns nothing at all.
2. `_fetch_pending_facts` renders the not-yet-consumed facts interval for Part 3.
3. The prompt `_PLAN_EXTRACTION_PROMPT` is rendered with the plan context and the facts section, then sent to a nudge agent (same builder, same `nudge: True` gate) with the conversation.
4. After a successful `ainvoke`, if a pending facts range existed, `_advance_facts_consumed` advances the consumed watermark.

The prompt produces three outputs:

- **Part 1: structured knowledge.** `knowledge(action="write", ...)` writes JSON documents under `config.path.PLAN_KNOWLEDGE_DIR` (`workspace/knowledge/plans/<plan-name>/`): `task-<position>.json`, `wave-<index>.json`, `plan-summary.json`. Each task carries `failure_set`, `success_path`, `method`; waves carry failure / success patterns; the plan carries overall method, key failures / successes, and reusable patterns.
- **Part 2: skill library update.** `skill_manage` patches a loaded or existing class-level skill, adds a support file, or creates a new class-level umbrella under `skills/auto/`. The prompt is explicitly active ("most completed plans produce at least one skill update") and lists user corrections, workflow corrections, non-trivial techniques, and outdated skills as first-class signals.
- **Part 3: persistent facts.** `memory(action="fact_add", target="<category>", content="<fact>")` writes to `facts/*.md`. This block is rendered only when a pending facts range exists; it extracts user preferences, project conventions, key decisions, and tool lessons, never temporary progress.

Reads are served later by the same tool (`knowledge(action="read")`), and a condensed plan summary is auto-injected into the system prompt by `build_knowledge_block` (`knowledge/prompt_block.py`).

### 4. Pre-compression memory flush (session-memory P0-1)

Both compression paths (`_apply_compression_under_lock` and `_aapply_compression_under_lock` in `agent/middlewares/summarization.py`) run the flush when a cutoff discards messages, before the summary is generated:

- `run_memory_flush_sync(...)` on the sync path;
- `await run_memory_flush(...)` on the async path.

`should_flush` (`agent/middlewares/memory_flush.py`) gates on `MEMORY_FLUSH["enabled"]` plus either `total_chars >= force_flush_chars` (50 000) or `estimated_tokens >= soft_threshold_tokens` (8 000). The flush is one `llm.ainvoke` / `llm.invoke` of `_FLUSH_PROMPT` over the about-to-be-discarded text, built by `_build_llm` with `model=MEMORY_FLUSH["model"]`, `max_tokens=2048`, `timeout=30`. It is **not** an agent and has no tools.

The response is parsed for `§`-delimited entries. An empty result or `(none)` writes nothing. Otherwise `MemoryStore.append_entries` routes each entry: entries matching `^\s*user\s*:` (case-insensitive) go to USER.md, everything else (Environment / Project / Decision / Tool / no prefix) goes to MEMORY.md. `append_entries` deduplicates against the target file and, unlike `add`, evicts the oldest entries to stay inside each file's own limit.

The flush only extracts cross-session facts. Temporary task progress is deliberately left to the summary. Any exception is logged and swallowed; the flush never blocks compression.

### 5. Post-compression todo fork

At the same compression point, `_schedule_compression_todo_update` (`summarization.py` -> `nudge.schedule_compression_todo_update`) schedules `update_todos_from_compaction` as a fire-and-forget `asyncio.create_task`. The scheduler gates on ALL of:

- `compression_todo_update_enabled` (`SUMMARIZATION`, default `True`);
- the cut actually discarded messages;
- no `compression_todo_update_lock` is already held for the session;
- the session has a non-empty todo list;
- an event loop is running (the sync compression path skips, logging at debug level).

`update_todos_from_compaction` runs a nudge agent with system prompt `_COMPRESSION_TODO_PROMPT` and exactly one tool: `_build_main_session_todowrite(session_id)`. The fork reconciles the current todo list against the discarded slice: it marks actually-finished items `completed`, abandoned or superseded items `cancelled`, adds evidenced new work as `pending`, and writes the COMPLETE list back in one `todowrite` call (full replacement, not a delta). If nothing changed it writes the list back unchanged.

The fork result messages are logged only. Nothing reaches the main graph or its checkpointer, and the derived session's middleware state is cleared in `finally`.

## Isolation & Safety

| Guard | What it protects |
|---|---|
| Nudge metadata allowlist (`metadata["nudge"]`, `_NudgeLimitTool` default) | Memory nudge and plan extraction may only call tools tagged for the nudge phase. Every other main tool returns an error `ToolMessage` instead of executing. |
| Compression metadata allowlist (`metadata["todo_update"] is True`, `_NudgeLimitTool(allowed_metadata_key="todo_update")`) | The compression fork admits exactly the metadata-marked `todowrite` shim. |
| Derived session key `<id>::compression-todo` | The compression fork's `IterationBudget` / `ToolGuardrails` / `ToolCallNormalize` state keys cannot collide with the main session, which is mid `awrap_model_call` while the fork runs. Only `compression_todo_update_lock` is deliberately written on the main session, as the cross-path re-entrancy coordinator. |
| Main-session-bound `todowrite` shim | The fork graph runs under the derived key, so the state-injected real `todowrite` would resolve the wrong session. The shim reuses the real tool's `args_schema` and `description` verbatim (zero schema drift), drops the injected `session_id`, and binds the main session id captured at build time. |
| Read-only forks, no checkpointer | Every nudge / extraction fork's result messages are logged and discarded. The forks have no checkpointer, so they cannot write the main graph's state. |
| Per-session re-entrancy locks | `nudge_review_memory_lock`, `nudge_plan_extraction_lock` and `compression_todo_update_lock` prevent overlapping runs of the same extraction path. While a nudge lock is held, `after_agent` skips the nudge decision. |
| Fail-open boundaries | Every path wraps its work in `try/except`, logs, and returns. No extraction failure propagates into a turn, a compression, or another extraction. |
| Background-task reference retention | `_BACKGROUND_TASKS`, `_COMPRESSION_TODO_TASKS` and `_COMPRESSION_TODO_TASKS` set hold strong refs so asyncio cannot garbage-collect an in-flight task. |

## Storage & Limits

| Store | Path (constant) | Limit |
|---|---|---|
| MEMORY.md | `config.path.MEMORY_DIR / "MEMORY.md"` (`workspace/memory/MEMORY.md`) | 2200 chars (`MemoryStore.memory_char_limit`) |
| USER.md | `config.path.MEMORY_DIR / "USER.md"` (`workspace/memory/USER.md`) | 1375 chars (`MemoryStore.user_char_limit`) |
| facts layer | `config.path.FACTS_DIR` (`workspace/memory/facts/<category>.md`) | 4000 chars per file, 5 categories: `environment`, `project`, `decisions`, `user_prefs`, `tool_lessons`; on-demand only, never injected into the system prompt |
| plan knowledge | `config.path.PLAN_KNOWLEDGE_DIR` (`workspace/knowledge/plans/<plan>/`) | `task-<n>.json`, `wave-<n>.json`, `plan-summary.json` per plan; prompt-guided field caps (150 / 100 chars) |
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
| `plan_extraction_enabled` | `CONTEXT_ENGINE_HOOK` (`config/features/agent_side/context_engine_hook.py`) | `True` | Enables todo-complete plan extraction |
| `nudge_memory_threshold` | `CONTEXT_ENGINE_HOOK` | `10` | Turns between memory nudges |
| `facts_char_limit` | `TIERED_MEMORY` (`config/features/agent_side/tiered_memory.py`) | `4000` | Per-category facts file cap |
| `facts_categories` | `TIERED_MEMORY` | `environment`, `project`, `decisions`, `user_prefs`, `tool_lessons` | Fixed category set |
| `compaction_cooldown_rounds` | `SUMMARIZATION` | `3` | Proactive-compression cooldown after an actual compression |
| `memory_char_limit` / `user_char_limit` | `MemoryStore.__init__` | `2200` / `1375` | MEMORY.md / USER.md caps |

## Verification

Focused tests:

```bash
uv run pytest \
    tests/agent/middlewares/test_compression_todo_update.py \
    tests/agent/middlewares/test_compression_cooldown_persist.py \
    tests/agent/middlewares/test_memory_flush.py \
    tests/agent/middlewares/context_engine/test_plan_extraction.py \
    tests/context_engine/facts/test_facts_extraction.py \
    tests/agent/tools/test_memory_store.py -q
```

- `test_compression_todo_update.py`: trigger gating, fire-and-forget scheduling, re-entrancy lock, fail-open release, prompt content, the `todo_update` metadata gate, and full-fork isolation (derived key, main-session `todowrite` shim, no checkpointer / message leakage).
- `test_compression_cooldown_persist.py`: cooldown survival across restarts.
- `test_memory_flush.py`: flush gating, routing, and non-blocking failure.
- `test_plan_extraction.py`: the four `_detect_todo_all_complete` branches, the `after_agent` 5-tuple contract, nudge dispatch, the facts yield, and `_build_plan_context`.
- `test_facts_extraction.py` and `test_memory_store.py`: extractor parsing / replay semantics and the two memory stores.

AI-judged evaluation: `evals/nudge_extraction/suite.py` seeds a completed-plan run (plan file, completed todos, synthetic subagent runs, a pending facts turn), invokes the real `_nudge_plan_extraction`, then asks an auxiliary LLM judge whether the produced skill is genuinely grounded in that run, reusable, and non-generic. Every write is redirected into the sandbox, so the real `skills/auto/` and `workspace/` are never touched.

```bash
uv run python evals/evals.py nudge_extraction
```

The suite checks `knowledge_written`, `facts_written`, `skills_created`, `ai_judge_skill_quality`, and `no_repo_pollution`.

## Known Boundaries

- **The compression fork is not given `todoread`.** Only the `todo_update`-marked `todowrite` shim is admitted; the fork receives the current list in its prompt, and `todoread` is intentionally left untagged (`agent/tools/todolist/tools/__init__.py`).
- **Memory flush extracts cross-session facts only.** Temporary task progress belongs to the summary, not to MEMORY.md / USER.md.
- **Facts extraction is at-least-once.** An empty fact list is a valid result and still advances the consumed watermark; a non-list LLM response raises so the watermark stays and the range replays. Replayed turns can produce duplicate facts, which `add_fact` deduplicates on exact text.
- **The cooldown suppresses proactive compression only.** `compaction_cooldown_rounds` (3) blocks T1 / T2 / T3 compaction after an actual compression; the T4 / T5 provider-error recovery bypasses the cooldown, the per-turn attempt cap, and the other anti-thrash gates by construction.
- **Plan extraction absorbs the facts interval.** On a plan-extraction turn the per-turn pipeline is skipped, and Part 3 covers the pending range in the same pass. The consumed watermark advances only when a pending range was actually injected and the pass succeeded.

## Related Documentation

- [Session Memory Architecture](../session_memory/README.md): P2-3 (facts) and P0-1 (memory flush) in the SESSION plan.
- [Summarization](../summarization/README.md): compression triggers and the cooldown that gates the flush and todo fork.
- [Long-Running Tasks](../long-running-tasks/README.md): TaskFlow and the todo planning layer that plan extraction reads from.
- [Middlewares README](../../agent/middlewares/README.md): ContextEngineHook and Summarization reference.
