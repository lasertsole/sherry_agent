# AGENTS.md — sherry_agent

AI coding assistant guide for the EMA AI Agent (Sherry) project. Read this before exploring the codebase.

## Project

**EMA AI Agent** — a role-playing AI agent with long-term memory, multi-level subagents, and a task orchestration engine. Python 3.13 (uv) · LangChain/LangGraph 1.3 · Robyn backend · Nuxt 4 + Tauri 2 frontend · SQLite (WAL) storage.

## Quick Commands

```bash
uv run python -m server                              # start backend (127.0.0.1:8080)
uv run pytest tests/agent/middlewares -q -k "not llm_e2e"  # run middleware tests
uv run --no-sync python tests/run_tests_split.py     # CI test gate (3-process; group C also runs the perf guards)
uv run --no-sync pytest tests/perf -q -s             # hot-path perf guards: prints each measured growth ratio
uv run --no-sync pytest tests/ -m "not llm_e2e" --cov=agent --cov=server --cov=context_engine \
  --cov=pub --cov=runtime --cov=workspace --cov-report=term-missing   # coverage report (single process; use run_tests_split.py -- --cov-append for the gate grouping)
uv run --no-sync python scripts/check_docs_parity.py # four-language README parity gate
uv run --no-sync python scripts/check_doc_links.py   # Markdown dead-link + anchor gate
uv run --no-sync python scripts/check_doc_language.py # docs carry their own language (en: no CJK; ja/ko: no Simplified-only)
uv run --no-sync python scripts/dev/mock_searxng.py 8888  # throwaway SearXNG stand-in (the exercise-the-fallback instance)
uv run --no-sync lint-imports                        # import-linter contract check
uv run --with ruff ruff check . && uv run --with ruff ruff format --check .  # lint + format
uv run --no-sync basedpyright agent/                 # type check
cd client && pnpm test:unit && pnpm test:integration && pnpm run dpdm  # frontend tests
./scripts/verify_feature.sh                         # one-shot feature verification (client typecheck/unit/integration/dpdm + repo pytest gate + docs gates; --quick for client-only)
```

## Directory Structure

| Directory                            | Purpose                                                                                                                                                                              | Key entry point                                      |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------- |
| `agent/`                             | Agent core: middleware chain, tools, subagent system, checkpointer                                                                                                                   | `agent/core.py::built_agent()`                       |
| `agent/middlewares/`                 | Middleware pipeline (summarization, guardrails, HITL, intent, continuation, message_persistence)                                                                                     | `agent/middlewares/__init__.py`                      |
| `agent/tools/`                       | LLM-callable tools (taskflow, todolist, memory, subagent, file, search, ...)                                                                                                         | `agent/tools/__init__.py::build_main_tools()`        |
| `agent/tools/taskflow/`              | Task orchestration engine (DAG, budget, deadline, progress, board, step judge, expectation→actual closure, evidence collector)                                                       | `agent/tools/taskflow/config.py`                     |
| `agent/tools/todolist/`              | Session-scoped todo planning layer                                                                                                            | `agent/tools/todolist/service.py`                    |
| `agent/tools/subagent/`              | Multi-level subagent system (spawn/registry/announce/sweeper, completion judge, functional roles)                                                                                    | `agent/tools/subagent/spawn/core.py`                 |
| `agent/tools/pub_base/`              | Shared tool infrastructure (`BaseSQLiteRepository` for the three SQLite stores, path utils, skill usage, the SQLite evidence ledger/recorder)                                                                             | `agent/tools/pub_base/sqlite_store.py`               |
| `agent/wrapper/`                     | Graph-level wrappers (repetition guard, context limit) + pluggable registry                                                                                                          | `agent/wrapper/registry.py`                          |
| `agent/state_port.py`                | The transcript port: read the live message list / pending HITL interrupt, heal an interrupted turn (server calls this, never the checkpoint API)                                     | `agent/state_port.py`                                |
| `agent/trajectory_events.py`         | Agent-side producers of trajectory events (reaches the server-side ledger through `runtime.hooks`)                                                                                   | `agent/trajectory_events.py`                         |
| `server/service/trajectory_store.py` | The per-turn trajectory ledger: one row per tool call (start/end merged), plus turn/compact/error events                                                                             | `server/service/trajectory_store.py`                 |
| `config/`                            | Centralized configuration (paths, features TypedDicts, schema, settings)                                                                                                             | `config/__init__.py`                                 |
| `config/features/`                   | Per-object feature config (87 TypedDicts)                                                                                                                                            | `config/features/__init__.py`                        |
| `server/`                            | Robyn HTTP/WS backend (trigger → service → queue/DAO → utils)                                                                                                                        | `server/__main__.py`                                 |
| `context_engine/`                    | Memory engine (MesMemory SQLite + curator)                                                                                                                                           | `context_engine/store/db.py`                         |
| `workspace/`                         | Live persona files (gitignored; templates in `workspace/template/`)                                                                                                                  | `workspace/prompt_builder.py::build_system_prompt()` |
| `pub/`                               | Shared utilities (message pipeline, retry, validators)                                                                                                                               | `pub/func/message/`                                  |
| `models/`                            | LLM/model wrappers (main, auxiliary, vision, embed, reranker)                                                                                                              | `models/LLMs/main_llm.py`                            |
| `runtime/`                           | Runtime state registers (`session/`; canonical keys in `state_keys.py`) + process-level services (`process/`, `lane/`) + dependency-inversion seams (`hooks.py`, `data_provider.py`) | `runtime/session/state_register.py`                  |
| `runtime/hooks.py`                   | Process-level callback registry — `agent`/`skills` resolve server-owned callables (auto-turn trigger, WS task table, skill scan) without importing `server`                          | `runtime/hooks.py`                                   |
| `runtime/data_provider.py`           | Prompt/skill-write provider registries (`PromptDataProvider`/`SkillWriteProvider`) — `workspace`/`context_engine` obtain agent-owned data without importing `agent`                  | `runtime/data_provider.py`                           |
| `runtime/lane/`                      | Process-level concurrency lanes (`Lane`/`LaneManager`/`lane_slot`/`LaneType`)                                                                                                        | `runtime/lane/core.py`                               |
| `tests/`                             | Mirror-structured pytest suite (markers: unit/integration/module/system/regression/perf)                                                                                             | `tests/run_tests_split.py`                           |
| `skills/`                            | SKILL.md skill system (builtin/auto/plugins)                                                                                                                                         | `skills/loader.py::scan_skills()`                    |
| `wiki/`                              | Four-language subsystem docs (published to the GitHub Wiki)                                                                                                                                                         | `wiki/long-running-tasks/README.md`                  |
| `wiki/context-governance/`           | Context governance docs (persistence, eviction, slices, overflow clip, summary filtering)                                                                                            | `wiki/context-governance/README.md`                  |
| `scripts/`                           | Dev tooling (git hooks, maintenance scripts)                                                                                                                                         | `scripts/hooks/`                                     |

## Architecture

```
User message → Robyn WS → agent.core.built_agent() graph
  │
  │    (A HEADLESS turn — cron / auto-turn / a system-injected carrier — has no
  │     operator: `resolve_turn_operator` returns None and the approval gates
  │     auto-deny. The tools that call `interrupt()` THEMSELVES bypass those
  │     gates, so each now refuses instead of suspending: `question` declines,
  │     `exit_plan_mode` returns an error telling the model to stop, the
  │     sherry-self-file approval fails closed, and the generic `interrupt_on`
  │     strategy plus the sandbox-bypass path deny with `NO_OPERATOR_MESSAGE`.
  │     The predicate is `no_operator_in_scope()` — an EXPLICIT empty scope, not
  │     "no scope at all" (a unit test or an auxiliary graph keeps the old
  │     suspend-and-wait behaviour). Boot rebuilding the flag for a session
  │     whose graph is still suspended lives in `server/service/boot_recovery.py`,
  │     together with the dangling-tool-call heal and the CLAIMED-row requeue.)
  ├─ middleware chain (before_agent → before_model → LLM → tools → after_model → after_agent)
  │    system_prompt_injection (@dynamic_prompt) → WorkspaceNotice → ToolSelection → MultimodalProcessor → IterationBudget
  │    → ToolGuardrails → ToolResourceLock → ContextEviction(P0-2/P2-4) → ToolCallNormalize → PathGuard → SubagentCompletionDrain
  │    → TaskIntent(E7) → DynamicReminderMiddleware → OutputRepetitionGuard → MaxTokensBoost → ThinkingControl → HeartbeatStaleness → HITL
  │    (TaskIntent's intent detection is RULES-ONLY: four-language rules — EN/ZH/JA/KO
  │     keywords plus question/chat patterns, then a length fallback over 100 chars.
  │     The old auxiliary-LLM fallback for the 20-100 char request-phrasing window was
  │     removed by the over-engineering audit; a short JA/KO request with no keyword is
  │     no longer armed — the conservative direction, fail-open to "not a task")
  │    → MessagePersistence → LLMRetry → Summarization → InvalidToolCallRepair → PlanContinuationEnforcer(E3; gates the plan = todos + open TaskFlow flows; delivered through the auto-turn trigger, which DEFERS past the ending turn's own `ws_task`/`answering` busy signals — up to `TODOLIST_INFRA["continuation_self_idle_wait_s"]` (30 s) — because the enforcer's hook runs while that stream is still draining; a directive still refused at the cap means a real user turn took over and is dropped. The client cannot stream that turn (it sends no `turn_started`), so `use-chat-stream.ts` refetches the newest turn page on its `done` — without that, the continuation ran but stayed invisible until a manual reload. The gate is BOUNDED by a consecutive-continuation ceiling: `mark_injected` counts every CONFIRMED delivery (a `busy`/`already_pending` answer from the trigger counts as none and is retried next turn end), and at `TODOLIST_INFRA["max_continuation_turns"]` (15) the gate delivers one final `[SYSTEM DIRECTIVE: CONTINUATION BUDGET EXHAUSTED]` directive and latches the session silent until a turn it did NOT inject — a real user message, or a subagent completion — resets the counter and the latch)
  │    (InvalidToolCallRepair is registered LAST = the innermost wrap_model_call
  │     layer, closest to the raw model call: a tool call whose `arguments` fail
  │     JSON parsing arrives as `AIMessage.invalid_tool_calls` with `tool_calls`
  │     empty, and the loop — which routes on `tool_calls` only — used to treat
  │     the message as a call-less final answer and END the turn silently
  │     (SWE-bench 20261017_003529: the django instance ended after 14.7 s with
  │     zero edits). The layer repairs the syntax (quote-only, byte-identical
  │     tail value; refuses ambiguous shapes; truncation-shaped misses are left
  │     to MaxTokensBoost) and re-attaches the call with its original id so the
  │     already-streamed `tool_start` frame pairs with the result; when nothing
  │     is repairable AND no valid call remains it re-calls the model with a
  │     corrective HumanMessage appended to the REQUEST only, bounded by
  │     `INVALID_TOOL_CALL["resend_max_retries"]`, stripping streaming callbacks
  │     like MaxTokensBoost does. Repaired calls still pass every wrap_tool_call
  │     gate; the middleware is in `scaffolding._MAIN_REQUIRED` AND
  │     `scaffolding._SUBAGENT_REQUIRED` (children carry it innermost too — their
  │     chain has no LLMRetry, so the resend re-calls the child's model directly),
  │     and the attempt is logged + recorded as an `invalid_tool_call` event)
  │    (WorkspaceNotice is a before_agent node: once per turn it compares the
  │     session's effective root with PROJECT_DIR_ANNOUNCED and the root's git
  │     branch+HEAD with GIT_HEAD_ANNOUNCED — what the agent was last told about
  │     — and, when either differs, splices ONE HumanMessage notice per change
  │     immediately before the turn's HumanMessage. Several switches coalesce
  │     into the final state, switching back sends none, the first turn baselines
  │     both silently; the notices are HumanMessages tagged origin/internal/
  │     provenance (the chat renders them as neutral cards, and
  │     pub.func.message.workspace_notice.is_workspace_notice lets the human-
  │     message scanners skip them), and the human message keeps its LAST place
  │     for TaskIntent and ContextEviction. It replaced the old switch-time
  │     aupdate_state announcement, which fired per switch)
  │    (DynamicReminderMiddleware injects the LIVE views as
  │     `<system-reminder source="…">` messages once per turn's first model call:
  │     plan mode, working directory, project instructions, todo list, boulder,
  │     TaskFlow board, continuity, knowledge index — the catalogue in
  │     `agent/middlewares/reminder_catalog/` decides what, at which priority, with
  │     which cooldown and under a 30 000-char total budget (lowest priority
  │     dropped first). Priority is the BUDGET order; delivery reverses the kept
  │     set so the highest-priority view (plan mode, then the todo list) lands at
  │     the END of the reminder block, immediately BEFORE the turn's user
  │     message — live state first, then the request, with the user's own input
  │     staying LAST (the reducer only appends, so the block is positioned with a
  │     `RemoveMessage(REMOVE_ALL_MESSAGES)` whole-list rewrite). A resumed turn
  │     that already carries the block before its user message skips
  │     re-injection. They used to be
  │     REBUILT INTO THE CACHED SYSTEM PROMPT,
  │     which froze them until the next rebuild and made the prompt a bad cache
  │     citizen; the prompt now holds only stable content (persona + memory +
  │     skills index), each reminder is re-derived from live state every turn,
  │     and a compaction resets the throttle state so nothing is lost with the
  │     compressed history. The three older injectors (TaskIntent steering,
  │     todo-continuation directives, workspace notices) keep their own paths —
  │     the catalogue is an overlay, migration is batched.)
  │    (MessagePersistence flushes tool results the moment they return via
  │     wrap_tool_call; after_model nodes chain in reverse registration order, so it
  │     is also the first after_model hook — new human/ai/tool messages reach
  │     MesMemory before HITL rewrites denials or interrupts. HITL denials are the
  │     exception: its short-circuit bypasses the wrap layer and lands next boundary.
  │     ContextEviction wraps outside MessagePersistence: the raw result is
  │     persisted first, then replaced by an evicted-head/tail preview before it
  │     reaches state; read_file results are sliced, never offloaded. A payload
  │     the 5+5-LINE window cannot shrink (ten lines or fewer — a single
  │     newline-free line, i.e. a compact JSON body, is the extreme — has
  │     overlapping windows and would be reproduced twice) falls back to a
  │     CHAR-budgeted head/tail (per-line 2 048, per-side 8 192 — the mirror of
  │     the fd path's byte budgets), so it is offloaded like any oversized
  │     result instead of riding the window in full. Its
  │     before_model tags an oversized trailing HumanMessage (full text stays in
  │     state/MesMemory) and wrap_model_call truncates only the model view)
  │    ThinkingControl: per-session thinking control (client switch / level
  │     picker → PUT /sessions/thinking → state register); swaps request.model
  │     for the variant built by build_main_llm(thinking=... / level=...) when
  │     the session carries an explicit LLM_THINKING_ENABLED flag, else env
  │     default. Levels are DECLARATIVE since the pi-style rework: the value
  │     domain is the shared EXTENDED_THINKING_LEVELS enum (off / minimal / low
  │     / medium / high / xhigh / max), models/LLMs/reasoning_payload.py
  │     resolves a per-model thinkingLevelMap + thinkingFormat and CLAMPS the
  │     request onto the nearest level the model accepts, and GET
  │     /sessions/thinking returns the model's `levels` list so the client's
  │     picker never offers an unsupported level. A session can also carry a
  │     CUSTOM wire mapping (GET/PUT /sessions/thinking/custom,
  │     StateKey.CUSTOM_THINKING_MAPPING with the usual parked twin): validated
  │     at the API boundary (lowercase dotted wire_field, alphanumeric values,
  │     >=1 level, budgets positive and under the context window) and it
  │     overrides the declarative map outright — it rides into the variant AND
  │     into the memory review's cache-warm rebuild (the published descriptor
  │     carries it). Anthropic has an adaptive branch, Bedrock splits Claude
  │     from GPT, and the headroom scales per level via
  │     REASONING_BUDGET["thinking_budgets"]. The middleware also refreshes the
  │     frozen memory snapshot when the request configuration changes: a
  │     model switch starts a new prefix-cache namespace and a thinking switch
  │     was measured to zero cache_read on an identical prefix (DeepSeek,
  │     2026-10-22), so `before_model` compares the turn's (thinking key,
  │     profile fingerprint, custom-mapping fingerprint) against the last
  │     applied one — process-level because the graph is rebuilt every turn —
  │     and, on a change, reloads `MemoryStore.load_from_disk()` and deletes
  │     `StateKey.SYSTEM_PROMPT` from both registers BEFORE
  │     `system_prompt_injection` resolves the prompt. The switched turn's
  │     first model call therefore sees mid-session FACTS/MEMORY/USER writes;
  │     first turns, unchanged controls, and reload failures keep the cache.
  │
  ├─ tools: build_main_tools() → taskflow(3 grouped over 16 actions) + todolist(2) + memory + subagent(8)
  │         + file_tools + web_search + web_fetch + terminal + python_repl + question + ...
  │
  ├─ graph wrappers: apply_graph_wrappers(inner)
  │    → ContextLimitGuard(RepetitionGuard(graph))
  │
  ├─ lanes: runtime/lane/lane_slot() gates every dispatch point —
  │    MAIN(turn) / SUBAGENT(child) / NUDGE(memory) / NESTED(sessions.send)
  │    (the SUBAGENT lane ALSO sits behind the load gate, below)
  │
  └─ subagents: spawn_subagent_direct() → accepted as PENDING when the
       SUBAGENT lane is full → RUNNING inside the lane slot (`started_at`
       stamped there) → detached child agents → announce pipeline → drain

     spawn carries a per-spawn WALL-CLOCK timeout (`sessions_spawn`'s
     `run_timeout_seconds`, clamped by the tool to `[600, 7200]`; internal
     callers bypass the clamp and keep their own bounds), and after a
     timeout/kill whose completion notice has no Result the caller can pull the
     child's transcript — `sessions_messages` reads its checkpoint by
     session_key (`agent/tools/subagent/tools/sessions_messages.py`, token-
     budgeted, spawner-only)

     Result-text caps scale with the serving model's output budget
     (`agent/tools/subagent/output_caps.py`): the stored result (24000 B),
     delivery (4000 chars), summary (2000), judge view (8000) and transcript
     read (4000/500 tokens) are calibrated at an 8000-token output window. A
     configured `*_MAX_OUTPUT_TOKEN=W` multiplies each by `W/8000` (8192 →
     ×1.024), while an unset value keeps the legacy numbers exactly. A
     per-role model profile carries its OWN `max_output_tokens` (decimal
     string, default 8192, min 8K) and that value is used both for the child's
     request and for these result caps — it no longer follows the MAIN env
     budget. Tier and depth-role children read the MAIN/AUXILIARY env cap. The
     effective limit is resolved at spawn and frozen on the run's
     `max_output_tokens`; completion, delivery, announcement, the completion
     judge and `sessions_messages` all read it.

     Checkpoint retention is latest-only per thread: a child's
     `_release_child_resources` prunes its thread to the newest top-level
     checkpoint + its writes (tools/model namespaces dropped), and a main
     `on_turn_finished` does the same for its session before the drain starts
     (HITL-pending sessions skip it). Deleting a main session cascades through
     every descendant run: suppress/kill, then delete each child's registry row,
     checkpoint thread, session dir, planning rows and attachments. After
     `init_registry()` a boot sweep deletes every orphan subtree whose root main
     session or parent run is gone, in one fail-open pass.

     A restart is a STATE TRANSITION, not a no-op (`registry/state.py`). No task
     survives it, so a restored RUNNING run is terminalized as a TIMEOUT with
     `pause_reason=process_restart` / `ended_reason=process_restarted` (leaving
     it RUNNING made every liveness reader — the WAITING-flow sweep, orphan
     recovery, `should_keep_child_link` — believe the child was alive), and its
     delivery stays PENDING for the sweeper's undelivered scan
     (`_scan_undelivered_terminal_runs`, throttled to one sweep interval,
     idempotent through the announce flow), which is how the owning step learns
     instead of staying DISPATCHED. Every persisted monotonic timestamp is
     REBASED onto the restore point (`_rebase_restored_clock`):
     `time.monotonic()` is process-relative, so an old value can read as hours
     old or as a future time and silently disable each `now - timestamp` expiry
     check; `archive_at` is a deadline and gets a fresh window rather than being
     rebased to "now" (which would archive on the first sweep). Killing an
     INTERRUPTED run also releases its per-session guard state
     (`release_child_session_state`) — there is no live child agent whose
     `finally` would.

     Spawns are ROLE-CONTAINED: a specialized child (researcher / executor /
     reviewer / librarian) may only spawn roles on its allow-list
     (`SPAWN_ALLOWED_CHILD_ROLES`, types/functional_role.py) — its own role
     plus, for an executor, the READ-ONLY researcher / librarian, so it can
     research before it writes; an absent entry means same-role only, MAIN and
     general stay unrestricted, and the allow-list only ever adds read-only
     roles — that single-hop rule is what keeps multi-hop safe (a write role
     reaches only write roles), so a read-only researcher still cannot hire a
     write-capable executor. The refusal names the allowed set and the way out,
     and `functional_role` rides on the tool for a same-role fan-out

  completion gates (all always-on, all fail-open):
    · Tier 1 schema gate — a step's response_schema is validated on
      taskflow_step(action="resume") (json parse + validate_structured_output); a miss is a
      retry signal ("schema_error") that never spends a judge call, and an
      unretried miss marks the step failed. The retry carries the validator's
      own message as its feedback (the judge never ran, so judge_feedback is
      empty) and the replacement inherits the step's spawn contract
      (_retry.step_spawn_kwargs → response_schema / functional_role /
      step_model / step_timeout_seconds — every dispatch site passes it,
      including the manual batch and the auto-dispatch wave). The shape is
      ALSO enforced at generation time: the child agent is built with
      create_agent(response_format=<schema>) (AutoStrategy: provider-native
      structured output, else a structured-output tool call), the schema
      text stays in the child's system prompt as the prompt-level fallback,
      and sessions_spawn exposes the same output_schema field so a
      tool-driven child can be constrained the same way
    · StepJudge — step_judge.py, on taskflow_step(action="resume") for criteria-bearing steps
      (judge_criteria or validation_criteria; pass / retry within
      STEP_JUDGE["max_retries"] / block); a Tier 1 pass never skips it, and a
      caller-declared step_outcome outranks both
    · CompletionJudge goal loop — every spawn, bounded by
      COMPLETION_JUDGE["goal_max_turns"] and gated by its master switch
      (COMPLETION_JUDGE["enabled"]; off = no judge call at all, whatever budget
      a caller passes); continue injects a follow-up turn. The judge's FIRST
      call is skipped when the opening turn produced a result AND the child
      session's ledger shows a passing, non-stale row — the ledger already
      carries the verdict a simple spawn would otherwise pay a model call for
    · Evidence ledger — always-on terminal/python_repl auto-record + file-edit
      stale events in SQLite (WAL; lazy retention + session/boot purge); each
      session accumulates edited paths in `verification_state` (project-relative
      normalization; capped 200, not reset per verification) and every verification snapshots them into
      `modified_files`; a write also broadcasts a `scope="cross_session"` stale
      row to every OTHER session whose evidence command names the path OR whose
      `modified_files` contains it (gated by EVIDENCE_CROSS_SESSION_STALENESS;
      the writer's own session is marked separately); at taskflow_flow(action="finish") the regression gate replays the flow's
      passed verification commands once (allowlisted, no shell, capped) and
      writes broken / unmark events, so a cross-file regression is confirmed
      while a false-positive stale is cleared — a rewriting formatter cannot be
      replayed at all (`black`/`prettier`/`ruff format` need `--check`/`--diff`;
      a bare invocation used to REWRITE files outside the license/CAS stack) — the replay allowlist is the code
      defaults PLUS `regression_gate_allowed_executables` from sherry.jsonc (the
      `yolo_deny_paths` list-setting pattern: consumer owns the defaults, user
      entries merge per call, so a new toolchain is a config line, not a code
      change; only the replay gate reads it, and the list API at
      `GET|PUT /sherry-config/list/<key>` accepts only the registered keys —
      `LIST_SETTING_KEYS` in `config/sherry_settings.py` — validating every entry
      for lowercase/no-shell-metacharacters/bounds, with the built-in defaults
      never removable); read side derives
      broken/stale using the exact `modified_files` set, falling back to the
      command substring for legacy rows (evidence_collector.py)
    · taskflow_flow(action="finish") gates A–C — DAG completeness, no unresolved steps
      (blocked/failed/skipped/cancelled are reported by id with their reason),
      the flow's evidence read by `flow_id` FIRST (step children are stamped at
      spawn; finish temporarily stamps the creator's `StateKey.FLOW_ID` and
      restores it in `finally`, so replay/creator rows carry the flow; stale /
      broken / unmark events auto-stamp from the target session's flow context)
      carrying no FAIL/[stale]/[broken]. A restart empties that read — the
      stamp is memory-only — so an EMPTY flow-scoped result falls back to the
      flow's OWN sessions (its step children plus the creator, never the whole
      session), which is the only way evidence that still exists can gate the
      finish. Then Gate E's goal judge (only for a flow with a description and
      at least GOAL_GATE["min_steps"] = 3 steps)
    · TaskFlow state-machine guards — `validate_steps_list` refuses a
      `depends_on` CYCLE at create/update time (depth-first over the
      already-validated id graph, so an A→B→A plan cannot pin a flow until the
      inactivity sweep); `mark_flow_superseded` BUMPS `expected_revision`
      (superseding makes the predecessor terminal, so a writer holding the
      pre-supersede revision must lose its CAS, not resurrect a cancelled
      flow); the stale-WAITING marker writes its payload with
      `touch_updated_at=False` (that column is the inactivity clock the sweep
      itself reads); `_auto_fail_finish` KILLS the detached children under
      `TASKFLOW_INFRA["auto_kill_on_detach"]` (a failed flow can never collect
      their results, so a live child only holds a lane), fail-open per child;
      and a run that ended TIMEOUT/KILLED auto-resumes its step with
      `step_outcome="failure"`, so the step fails instead of being marked done
      with an empty result and unlocking its dependents
    · SubagentCompletionDrainMiddleware — unconditional programmatic gate on
      drained completion carriers when the session lacks passing evidence; a
      `[stale]` or `[broken]` row counts as "lacks" (the same marks the finish
      gates read), so a child whose work a later write invalidated cannot drain
      as if it were verified
```

## Queued User Input (`server/service/input_queue_service.py`)

A message sent while the session is busy is persisted as a `QUEUED` row in
`user_input_queue` (SQLite, `server/queue/user_input_queue.py`; cap =
`INPUT_QUEUE["max_active_per_session"]`, dedup by `client_msg_id`) and answered
by the `queued` WS frame with its FIFO position. The composer says so: while a
turn runs, `inputBox.vue`'s placeholder switches from 请输入内容 to
`chatInput.queueHint` (继续输入消息以排队 — four languages in the component's
inline block), and that hint is skipped when the box is disabled (a HITL prompt
takes the input slot; the disabled text wins). The turn runner drains those
rows **one per turn** (`server/service/turn_runner.py::_drain_loop` claims a
single row via `claim_next` and `_execute_single` drives it), so N queued
messages produce N turns and N replies — one answer per user bubble — never one
merged answer. `on_turn_finished` kicks the drain; a HITL-pending session drains
nothing until the resume completion.

Each queued row is manageable from the client (`cancel_queued` / `edit_queued` /
`send_now` frames → `queued_cancelled` / `queued_updated` / `send_now_ack`
`{event, session_id, msg_id, ok}` acks): cancel voids a `QUEUED` row (a
`CLAIMED` one is already inside a turn and is refused), edit rewrites only a
`QUEUED` payload, and `send_now` stamps the row's `priority` to 1 **before**
cancelling the running turn — the cancel-triggered drain then claims the
prioritised row first (`claim_next`'s `ORDER BY priority DESC, created_at ASC`
stays the single ordering rule; the row keeps its real `created_at`).

## Interrupted-Answer Continuation & Message Edit (`server/service/resumable_breakpoint.py`)

Two chat-row actions on the SAME per-session agent socket: **继续生成** (resume an
interrupted pure-text answer) and **编辑消息** (rewrite a sent user message and
re-run that turn). Both are additive WS frames; both fail open with one ack.

**Continue.** The cancel / heartbeat-timeout path already heals the checkpoint
and writes an `[interrupted] {partial}` marker (`interrupt_marker.py`). What was
missing was identity: `_record_resumable_breakpoint` (in `server/service/messages.py`)
stores `StateKey.RESUMABLE_BREAKPOINT` — mem + DB dual-write, like the token
budget counters — as `{marker_id, partial_text, reason, at_turn_id, turn_seq,
is_reasoning_only}`, and ONLY when the write was fresh (`wrote_checkpointer`),
`healed_calls == 0` (a dangling tool call needs a rerun/skip decision this MVP
does not make) and the turn produced visible text or reasoning. `turn_seq` is
the human-message count the marker id was derived from — the validation
fingerprint. `StreamDriver` adds an additive `resumable` boolean to `stopped`
(the cancel path and the WS stop ack) and to `done` (a heartbeat timeout ends
the turn NORMALLY, so the button must not need a `stopped` frame).

A new turn clears the breakpoint: `_GenerateTurn.__init__` (all generation turns,
the queue drain, the auto turn) and `_ResumeTurn.__init__` (HITL resume), gated
by the class flag `_clears_resumable_breakpoint` so `_ContinueTurn` keeps it for
itself. The client's `stopped`/`done` handler cannot rely on `streamingTurn` —
the user's own Stop clears it before any frame arrives — so the `ws:resumable`
handler falls back to the transcript's tail row.

`continue_generation` (WS frame) VALIDATES before streaming: no live task
(`busy`), breakpoint present and still the trailing marker with a matching turn
count (`stale`, which also drops it). Then `_ContinueTurn(_GenerateTurn)` runs
with `_is_continuation = True`: `_prepare` re-validates against the live
transcript, CONSUMES the breakpoint (one shot), rewrites the marker to the clean
partial text through `agent/state_port.py::restore_interrupt_marker` (same id →
`add_messages` upsert; refuses an absent id instead of appending) and picks
`_REASONING_ONLY_PROMPT` from `is_reasoning_only`. The rest is the existing
max-tokens continuation: the injected prompt is a `HumanMessage` tagged
`metadata={"internal": True, "provenance": "continuation"}`, and
`message_persistence._is_persistable` filters that provenance so the prompt
never shows up as a user row (this was pre-existing max-tokens behavior too).
A cancelled continuation writes its own breakpoint through the inherited cancel
hooks (the continuation prompt advances `turn_seq`, so the next marker id
differs). The client sends `continue_generation`, reads the ack
(`ok`/`busy`/`stale` → toast + hide + history reload), appends the chunks to the
interrupted row (stripping the marker prefix locally) and lets `done` reload
history — after which the persisted marker row and the continuation row are two
rows (accepted; MesMemory is append-only).

**Edit.** `edit_message {turn_num, new_text, msg_id, row_id?}` is refused while
`detect_state(session_id).busy` (a live turn or a pending approval owns the
transcript). `context_engine.store.core.find_user_turn_cut` maps the clicked
row to `(ordinal, cut_turn_num)`: `origin = 'user'` human rows only, so the
ordinal matches the checkpoint's human count while `cut_turn_num` is the
persisted BATCH number — they are NOT the same thing (`add_messages` starts a
new `turn_num` per persistence batch, so one user turn spans several numbers).
`agent/state_port.py::truncate_messages_after_turn` then removes that ordinal's
human message and everything after it from the live checkpoint through
`RemoveMessage` (the checkpoint is pruned to latest-only, so history rewind is
not available), counting turns over non-`metadata.internal` humans (reminder
blocks / notices belong to the turn that follows them); `-1` = the turn was
compacted away (`compacted`, never a silent partial truncation).
`store.core.delete_messages_from_turn` hard-deletes `turn_num >= cut` and
re-points `session_leafs` at the previous turn (first turn → leaf cleared), so
the re-run's batch reuses the edited turn's number. The edited text is then
re-submitted through `submit_user_input` with a FRESH `msg_id` (the old one is
tombstoned by the dedupe) — the normal `turn_started`/`chunk`/`done` flow
follows, and only failures ack (`edit_message_ack`). The client truncates
`chatMessages` at that turn, appends an optimistic user/AI pair with the SAME
turn number, and purges the Dexie cache from that turn on
(`clearCachedMessagesFromTurn`) — without that purge the local cache merge
resurrects the deleted rows on the next history load. Edit mode is entered from
the bubble's pencil (persisted user rows only), prefills the composer
(`setDraftText`), shows a banner and routes the composer's send to `handleEdit`;
Esc cancels.

## Chat Selection Quote & Side Session (`ChatBox.vue` / `SideChatPanel.vue`)

Right-clicking a **valid text selection inside one message row** opens a PrimeVue
`ContextMenu` anchored at the pointer. A right-click with no selection (or on a
control) does NOT call `preventDefault`, so the browser's native copy menu still
works.

- **Guard** — `client/app/composables/use-chat-selection.ts::inspectSelection(root)`
  returns `null` for a collapsed/empty selection, more than
  `MAX_SELECTION_CHARS` (8 000) characters, a selection outside the chat list,
  one starting in `button/input/textarea/select/[contenteditable=true]/
  [role=dialog]/[role=menu]/[data-hitl-card]/.p-contextmenu`, or one whose
  common ancestor has no `[data-message-id]` (a selection spanning two rows).
  `ChatBox` adds `data-message-id` / `data-message-role` to each message row;
  `selection-enabled` is false while a HITL card owns the input (the page passes
  `!hitlRequest`), and `allow-side-chat` is false inside the side panel.
- **添加到当前会话** — `ChatBox` emits `selection-add`; the page calls
  `inputBox.addQuoteChip(text, sourceLabel)` →
  `use-text-selection.ts::insertQuoteChip` inserts an atomic
  `<span contenteditable="false" class="text-quote-chip" data-quote-text="…">`
  (the label is a 40-char preview). `extractTextForSend` now turns that chip into
  a markdown blockquote (`> line` per line) and never leaks the preview; the
  file-mention branch is unchanged and still extracts `@path`.
- **在辅助会话中提问** — the SERVER creates the child
  (`POST /sessions/side_chat`, `server/service/side_chat_service.py`) as a
  COMPLETE FORK of the parent agent (NOT a subagent): its first turn is seeded
  with the parent's checkpoint transcript — including tool/browser results that
  never reached the message store — and the parent's per-session settings
  (main model, thinking, custom mapping, search backend, budget cap, agent
  config, project directory + its announced directory/git baselines, access
  mode) are copied. The copied messages carry
  `additional_kwargs["side_chat_context"]`, which `MessagePersistence` filters,
  so the child's visible history holds only its own new turns. The fork is cut
  at the parent's last completed assistant answer, so a dangling tool call (a
  pending HITL approval or an interrupted run) is never replayed into the
  child. Creation is IDEMPOTENT: an optional `client_request_id` in the POST
  body — the same (parent, key) pair answers the SAME child with
  `deduplicated: true` instead of a second complete fork (the client reuses one
  key per parent for 30 s, so a re-click after a lost response cannot duplicate).
  The link lives in `src/data/side_chats.db` (`fork_mode=1`; older text-
  snapshot links keep the bounded fallback) and the child appears in the normal
  session list as an ordinary session (its visible history still starts empty),
  so a closed panel is recoverable by reopening it. `createSideChatSession`
  (`use-side-chat.ts`) also copies the parent's preset binding and locked
  character snapshot into Dexie, caches the child's placeholder meta and
  announces it via `session:created` (so the list row appears immediately; a
  later list refresh replaces it with the server-derived title), then opens a
  session-scoped `sideChat` right-sidebar tab whose payload carries
  `{sessionId, quote, source, parentSessionId, inherited}`. The
  panel reads that payload on mount (no mitt race) and prefills the quote chip.
  `SideChatPanel.vue` reuses the page's lifecycle / draft-persistence / chunk /
  HITL / chat-stream slices over the child's own persistent socket, history and
  approvals; it has a compact question/plan/generic approval card. The child is
  PARENT-SCOPED, not tab-scoped: closing the right-sidebar tab only detaches its
  socket (`detachSideChatSession`) and leaves the session, its checkpoint, its
  model/thinking settings and its Dexie display caches untouched. Only deleting
  the parent destroys it — the server cascade cancels its turn, closes its
  browser page, purges its subagent subtree and every store (messages,
  checkpoint, queue rows, trajectory, writes, session dir), then drops the link.
  The client lists the parent's children first
  (`GET /sessions/side_chat?parent_session_id=…`) so it also clears the Dexie
  caches of side chats whose panels were already closed. `DELETE
  /sessions/side_chat` remains as an explicit purge route (not called by the
  tab's X), and a boot sweep removes links whose parent is gone. A side chat can
  never parent another side chat. The strip's down-arrow button
  (`RecentClosedTabsMenu.vue`, the LEFTMOST control in the tab-strip row) opens
  a two-tab menu (当前会话 / 全局) of recently closed tabs: closing a tab records
  it in `rightSidebar.recentClosed` (per parent session for side chats, per
  active session for other session tabs, all global tabs for every session;
  persisted). Each session group keeps its newest 10 pages and the global group
  its newest 10; older entries are dropped, and a destroyed session's entries
  are forgotten (single or batch delete), so another session never sees them.
  Restoring a side chat reopens the panel on the SAME child session with its
  history, using `prefillQuote: false` so the original quote chip is not
  inserted again. The 当前会话 tab group is PER SESSION: every session-scoped tab
  carries its owning `sessionId`, `setActiveSession` (driven by the shell's route
  watch) shows only that session's tabs and restores its last active one — or no
  tab at all — so switching to a new session never displays the previous
  session's tab; GLOBAL tabs remain shared and stay active across switches.
  `forgetSession` drops a deleted session's tabs and recent entries.
- **i18n** — menu and chip labels are `chat.selection.*` (central locales); the
  side panel's HITL copy mirrors `[sid].vue`'s inline `hitl.*` block.

**会话分支 / Branch (`fork_turn_num`).** A branch is the same fork machinery
with its fork point parameterized. `POST /sessions/side_chat` accepts an
optional `fork_turn_num`; the link table stores it, and an older
`side_chats.db` gains the missing columns on first connect (additive
`ALTER TABLE` guarded by a PRAGMA check — the VERY first release lacked
`client_request_id` too, and a file without it 500'd every side-chat request
with "no such column", live-verified; old rows read NULL = the legacy "last
completed answer" point). `create_side_chat` validates it (int >= 1; a bool is
not a turn) and answers it back, dedupe included. The incoming number is the
clicked AI row's PERSISTED `turn_num`, which is a per-BATCH number — one user
turn spans several batches (user=1, tool=2, answer=3) — while the checkpoint
cut counts USER turns; `create_side_chat` therefore translates it with
`context_engine.store.core.find_user_turn_cut` (the message edit's own helper)
BEFORE storing it, and an unresolvable row (compacted away) degrades to the
legacy cut. Storing the raw batch number silently misses the checkpoint and
falls back to the full text snapshot (live-verified: the child then answered
with the parent's SECOND turn too). `_history_up_to_turn` cuts the parent
checkpoint after turn N's last call-less AI answer, counting NON-internal
`HumanMessage`s —
reminder blocks and workspace notices belong to the turn that follows them, so
the count matches the client's `turn_num`. A turn cut off mid-tool has no safe
answer boundary, so the cut stops BEFORE that turn's work instead of replaying
the parent's dangling tool call; a compacted turn yields an empty cut and falls
back to the bounded text snapshot. `fork_turn_num=None` keeps a quote side chat
byte-for-byte. The UI entry is the AI bubble's 分叉 button (hover, beside copy;
emits the row's turn number) — unlike a quote side chat it opens no panel: the
page creates the child and `router.push`es to it. It is hidden while a turn
streams (the parent checkpoint must be settled) and inside a child session
(`GET /sessions/side_chat/link` once per session load; the server refuses a side
chat parenting another, so a fork cannot be forked). The client's idempotency
key is scoped per CREATION (`sidechat:<parent>[:fork:<turn>]`, 30 s reuse
window), so forking two turns never collapses into one child. The left sidebar
marks branched rows from ONE bulk read (`GET /sessions/side_chat/links`) and the
branch badge navigates back to the parent; a branch child's visible history
starts empty exactly like a side chat's (the inherited transcript is model-only).

## Live-Stream Safety Filter (`agent/middlewares/safety_filter/`)

Three layers, OFF by default (`sherry.jsonc` `safety_filter.*`, read LIVE through an mtime cache — the 系统配置 → 直播安全 tab flips them from a switch, so the cache is one `stat` in the steady state). The app ships **no word list**: the rules file (`safety_filter.keyword_file`, inside the workspace) is operator-maintained, because choosing one is a legal decision this project does not make.

- **Rules** (`rules.py`): `{pattern, type: exact|contains|regex, action: block|replace|warn, scope: input|output|both, replacement}`; validated (count / pattern / replacement caps, regex compiles) before writing, compiled with an mtime cache so an edit applies without a restart, and fail-open (a broken file reads as no rules).
- **Input** (`InputSafetyFilter`, `awrap_model_call`): a `block` hit returns `safe_refusal_message` WITHOUT calling the provider; `warn`/`replace` pass through and only notify (the input side never rewrites the user's own text).
- **LLM review** (`LLMModerationMiddleware`, inner to the keyword filter so a keyword block never pays for it): needs ALL THREE of the master switch, a non-`off` strategy (`on_keyword_hit` / `always`) and a model name — `safety_filter.llm_moderation_model` EMPTY keeps the review off whatever the strategy says, and the tab's switch is disabled until one is typed. The call reuses the auxiliary family's provider/credentials with the name overridden (`build_auxiliary_llm(model_name=…)`) and FAILS OPEN: timeout (20 s), request failure or an unparseable verdict lets the turn through.
- **Output** (`OutputSafetyFilter`, `aafter_model`): registered between `LLMRetry` and `Summarization`, so the reverse `after_model` chain runs it BEFORE `MessagePersistence` stores the row — `block` replaces the whole answer, `replace` substitutes the hit, `warn` only reports; the row is upserted with the same message id.
- **Streaming** (`stream_guard.py::StreamTextGuard`): holds the last `stream_buffer_window` (20) characters and releases ONLY scanned text. A `block` anywhere in the buffer latches (buffered text dropped, the replacement notice emitted once), a `replace` ending inside the releasable prefix is substituted, one the release point would cut in half holds the release back. A pattern longer than the window can only complete in the tail, so at most `len(match) - 1` characters can be visible before the end-of-stream flush cuts. A streamed answer therefore never reaches `OutputSafetyFilter` — the guard scrubs chunks before they become frames, so the client and the persisted row agree (`StreamTurn._safety_guard`, re-armed per phase).
- **Surface**: `GET|PUT /safety-filter/rules`, `POST /safety-filter/test`, `GET /safety-filter/log` (`server/trigger/http/safety_filter.py`); switches/model ride `PUT /sherry-config`. Every decision records one bounded per-session event and pushes a `safety_filter` WS frame; the client toasts it (block = error level). Tests: `tests/agent/middlewares/safety_filter/` + `SafetyFilterPanel.integration.test.ts`.

## First-Run Onboarding (`OnboardingDialog.vue` / `use-app-flags.ts`)

A four-step tour (welcome → preset → core controls → ready) shown once per INSTALL, not once per visit: the shell checks `onboarding_completed` at boot and the tour writes it on BOTH exits (开始使用 and 跳过). The flag lives in Dexie (`appFlags`, schema v6, via the `readAppFlag`/`writeAppFlag` wrappers) with a **localStorage mirror** that is read FIRST — a browser whose IndexedDB is blocked (hardened profile, private mode) would otherwise resurrect the tour on every entry, which is exactly the failure the mirror prevents; a failed write to either store never throws.

`createSessionFromPreset` (`persona-catalog.ts`) is the ONE session-creation orchestration the tour and `NewSessionPresetDialog.confirm()` share (apply preset → bind preset → character snapshot → placeholder meta → `session:created` → `localePath('/home/<sid>')`); the caller injects `ensureCharacter` (the page's helper) so the catalogue module never imports a component. 跳过 records the flag and opens the ordinary preset dialog (the previous first-run behavior); a failed 开始使用 keeps the dialog open, shows the error and leaves the flag UNSET so the tour returns next boot. The dialog is not mask/Esc dismissable (no stray click can silently drop the user into an empty UI).

## Keyboard Shortcuts (`use-shortcuts.ts` / `plugins/shortcuts.ts` / `ShortcutSettingsSection.vue`)

One binding table (`SHORTCUT_BINDINGS`) declares every chord with its built-in default; the EFFECTIVE chord is `overrides[id] ?? default`, so a rebind needs no re-registration. Two scopes with different owners:

- **composer** (`send-message` Enter, `history-prev`/`history-next` ↑/↓, `cancel-input` Esc): `inputBox.vue` carries ONE `@keydown.stop="onComposerKeydown"` and matches with `matchesBinding(event, id)` — the compile-time `.enter/.up/.down/.escape` modifiers are gone. A matching chord is `preventDefault`ed and dispatched; everything else FALLS THROUGH, which is what keeps Shift+Enter's native newline and typing intact (`@keydown.stop` without `.prevent`). The defaults reproduce the old modifiers byte-for-byte.
- **global / navigation** (`command-palette` Ctrl+K, `find-in-conversation` Ctrl+Shift+F, `new-session` Ctrl+N, `toggle-sidebar` Ctrl+B): ONE document listener installed by `plugins/shortcuts.ts`, which also loads the overrides at boot. Each owner registers its ACTION with `registerGlobalHandler(id, fn)` — the shell owns palette/new-session/toggle-sidebar, a session page owns find-in-conversation (registered on activation, released on deactivation; the registry is last-writer-wins, and `unregisterGlobalHandler(id, fn)` only removes a handler its caller still owns, so a cached page cannot unhook the visible one).

`ctrl` means the platform PRIMARY modifier: `comboFromEvent` folds Cmd into it, so one table serves macOS and Windows/Linux, and `formatCombo` renders ⌘⇧F vs Ctrl+Shift+F (the send button's tooltip is built from it, so it follows a rebind). Overrides persist in Dexie (`shortcuts` table, schema v5) as `{id, keys}` JSON; deleting the row restores the default.

The UI is the **快捷键 tab of 菜单 → 系统配置** (`ShortcutSettingsSection.vue`, appended LAST in `ConfigPanel.vue` — the panel's footer/embedder logic is keyed by tab INDEX — and it saves on its own, like the language tab). Capture runs as a WINDOW CAPTURE-PHASE listener with `stopImmediatePropagation`, so the chord being recorded never reaches the app's dispatcher (recording Ctrl+K must not open the palette); Esc cancels the capture, a bare modifier is ignored, a chord another binding already owns is refused (red "与 X 冲突，未保存") and the row stays in capture.

Not migrated on purpose: component-local `@keydown` on list items / overlays (they are internal UI interactions, not user-rebindable commands) and `BrowserPanel`'s CDP key forwarding.

## In-Conversation Search (`use-conversation-find.ts` / `ConversationFindBar.vue`)

Ctrl/Cmd+Shift+F opens a floating find bar over the chat. It searches the LOADED transcript (`chatMessages`), never the DOM: a turn's tool cards and intermediate text sit folded behind the final answer, and virtual rows outside the window are not mounted at all, so the in-memory list is the only complete view of what the reader can reach. Matching is plain case-insensitive substring per message with one entry per OCCURRENCE (a message may hold several), stepping wraps at both ends, and a query with no hits reads `0/0` with a red box and disabled arrows — an empty query cannot throw. The scan is debounced 300 ms in ChatBox, not in the bar: the bar's input must stay instant while the walk is O(N) over up to `MAX_LOADED_ROWS` (400) rows.

- The bar (`components/chat/ConversationFindBar.vue`) is a controlled view: query, matches and index belong to `useConversationFind`; the bar owns only its draft string and focuses/selects itself on open. It is absolutely positioned over the list so opening it never re-flows a single virtual row.
- The PAGE owns the gesture (`index/[sid].vue::onFindKeydown`, window listener): every KeepAlive-cached session keeps a ChatBox mounted, so a per-instance listener would open a bar in a session the reader cannot see — the handler answers only while the page's `isActive` is true. ChatBox exposes `openFind()`; Enter/Shift+Enter and the arrows step, Esc closes and forgets the query.
- A jump (`revealMessage`) does what `scrollToMessage` already did (scroll the row in, ring it for 2.4 s) plus two things it needed: it expands the target turn's FOLD (`expandedTurnHistory.add(fold.key)` — otherwise a match in folded history would be ringed on a row the fold hides) and it expands the target tool card. The ring is drawn on the message BUBBLE as well (`ring-2 ring-theme-main/50`): `focusedMessageId` used to have a visible effect on tool cards only, so a text match in an answer would have landed with no marker at all.
- A match in a row the memory cap already RELEASED is not in `chatMessages` any more: `jumpToMatch` therefore asks the page for `resetExhausted()` + `loadOlderHistory()` once and retries; no row growth ⇒ give up (the occurrence no longer exists anywhere the reader can scroll to).
- Labels are the central `chatFind.*` keys (four languages). Deliberately NOT done: regex, cross-session search, searching tool args/results, and the plan's advanced full-text `<mark>` highlighting (markdown output makes DOM text-node mapping non-trivial) — the MVP marks the current match by the ring alone.

## Floating Conversation Column (`index.vue` / `common/floating-window.ts` / `stores/chat-float.ts`)

The middle column can leave the shell's flex row and become a draggable window (ZCode's side-pane full-view float applied to the conversation). Entry is the toolbar button IMMEDIATELY RIGHT of the collapse-left button (the folder button shifts one seat right), shown only at viewport ≥ 640px; the same button toggles back, and the window's own header carries an explicit 恢复原样 button. While the window is out, BOTH sidebar collapse buttons (left and right) are hidden — the window owns its restore control; Ctrl+B still closes it and collapses the left column.

- **Three presentations**: `docked` / `floating` / `input-only` (plus the existing `hidden` column state). `chatFloat` (`floating` + `anchor` + user `width`/`height` + `collapsed`, persisted to localStorage) is the only state; the middle column binds `data-presentation`, class `fixed` vs `relative` (never both — same-specificity Tailwind utilities) and `minWidth: 0` while floating (the 35% viewport floor would otherwise win over the computed width).
- **Single instance**: the same middle-column DOM and the KeepAlive'd session page relocate, never remount — draft, scroll, streaming turn and HITL survive. No Teleport (moving scrollable DOM resets the scroll position).
- **Geometry** (`common/floating-window.ts`, pure functions): the default size is 45% × 78% of the shell clamped to 480–760 × 420–820; a user-resized size uses the wider drag band 360–1200 × 260–900, always clamped to the slot; 16px margin, 9 anchors (default `center`); drag threshold 4px; drag positions coalesce through one rAF into a `shallowRef`; release snaps to the nearest anchor. The anchor and the size are stored; a resize also keeps a transient top-left (it wins over the anchor until the next header drag).
- **Edge resize**: three thin grips — left/right change the width (the opposite edge stays put), the bottom changes the height. The collapsed toggle-state is separate from the size.
- **Input-only bar**: dragging the bottom edge below the 260px expanded minimum snaps to the input-only presentation — the window height becomes the measured composer dock (`[data-test="chat-composer-dock"]` in `[sid].vue`, which wraps the attachment previews, HITL/edit banners and the input box), the float header AND the shell toolbar unmount, and the composer's bottom stays where it was. The normal window's header carries a one-click 收起为输入框 button (next to 恢复原样), and a visible 展开对话 pill just above the bar is the way back (bottom-anchored, restoring the stored/default height). Width/height/collapsed persist across reloads; docking resets `collapsed` so the next open is the normal window.
- **Side columns**: the freed width is shared **equally between the EXPANDED side columns only** (one expanded → it takes all; none → nothing grows; a maximized right panel counts as unexpanded). `extraWidth` props add it to the rendered width only — no store is written, so docking restores both columns exactly. Their resize grips hide while a share is applied.
- **Auto-collapse** is suspended while floating (`useAutoCollapse`'s `isSuspended`); docking calls its returned `measure()` after the layout settles (the resize path was paused).
- **Right sidebar maximize linkage**: maximising auto-floats the conversation. At the moment it expands, capture `{leftExpanded, middleFloating}` BEFORE floating; on restore keep floating only when the left was expanded AND the conversation was already floating, otherwise dock. The snapshot is transient (not persisted); a boot with a persisted `maximized` uses the boot state as the snapshot, and the maximize path calls `expand()` so a persisted collapsed flag cannot leave an empty overlay with no restore button.
- **Clipping**: the floating root must NOT use `overflow-hidden` — the composer toolbar's upward `ToolbarPopover` panels (`absolute z-50`) would be cut; rounded corners come from the root background plus the header. The chat's own scroller keeps its own overflow.
- **Stacking**: the window sits at `z-50`, ABOVE the right sidebar's maximized overlay (`z-40`), so it stays visible while that panel covers the shell. Its own toolbar popovers (`z-50`) are inside the window's stacking context; PrimeVue dialogs/menus are teleported to the body and stay above both.
- A boot with `floating` persisted restores the window; the maximize watcher's restore branch ignores its initial `false` (no snapshot), or every reload would dock it.

## Voice Input (`use-voice-input.ts` / `server/service/audio_transcribe_service.py`)

The composer's mic button (`inputBox.vue`, in the `sendActions` cluster) records,
transcribes and inserts the text at the caret — the whole flow lives inside
`inputBox.vue` (`insertVoiceText`, exposed beside `addQuoteChip`), so `[sid].vue`
and `SideChatPanel.vue` get it without wiring. **Capture** is `MediaRecorder`
(mime picked from an `isTypeSupported` chain: webm/opus → ogg/opus → mp4) with a
300 s auto-stop cap and a `<0.3 s` refuse; the blob is then decoded by the SAME
browser that recorded it (`AudioContext.decodeAudioData`) and re-encoded to a
16 kHz mono PCM16 WAV (`encodeWavPcm16` / `resampleLinear` / `mixdownToMono`).
The WAV detour is deliberate: the STT daemon's decode chain (soundfile →
audioread) cannot read webm/opus and the packaged backend ships no ffmpeg,
while a browser always decodes its own container — the server keeps zero
decode dependencies. **Transport** is one gateway proxy over the local FunASR
SenseVoice daemon (`skills/builtin/core/speech_to_text/scripts/server.py`,
`127.0.0.1:9011`, file-path contract, no CORS/auth — unreachable from a
browser): `POST /audio/transcribe` parks the upload under `TEMP_DIR`, drives
the daemon through the skill client's public aliases (`daemon_alive` /
`spawn_daemon` — one spawn implementation, one `.stt_daemon.pid`), and unlinks
the file; `GET /audio/transcribe/status` reports
`{model_dir_present, daemon_ready}`. **Availability is the weights**:
`MODELS_DIR/STT_model/model_weight` on disk gates RECORDING (there is no
auto-download and the `.env` `STT_*` keys have no consumers), while
`daemon_ready: false` is just "cold" — the daemon spawns on first use, so the
route answers a cold start with `503 {warming: true}` (the client retries for
~60 s) and a missing deployment with `503 {unavailable: true}`. `null`
availability (backend unreachable) keeps the button clickable so the first
attempt surfaces the real error. An UNCONFIGURED mic stays clickable too — a
disabled button cannot speak — so the click toasts `chatInput.voiceUnavailable`
and no recording starts; the tooltip carries the same text. Insertion failures
never touch the box's content; `chatInput.voice*` keys cover the four languages.

## Project Directory Binding (`runtime/session/project_dir.py`)

Every session has a **project directory**: the root its tools resolve relative
paths against. Precedence is session binding → `SHERRY_PROJECT_DIR` →
`sherry.jsonc`'s `project_dir` key → `ROOT_DIR`, so with nothing configured the
behaviour is byte-for-byte the old behavior. `PUT /sessions/project` binds (or
clears, with `null`) the session's own value; a choice made while a turn is
running is PARKED (`PROJECT_DIR_PENDING`) and promoted by
`turn_runner.on_turn_finished` at the turn boundary — the same park/promote pair
the thinking/model controls use, including the HITL deferral.

The agent is told about a change **at send time**, not at switch time:
`WorkspaceNoticeMiddleware` (`agent/middlewares/workspace_notice/`) runs one
comparison per turn over two facts — the effective root against
`StateKey.PROJECT_DIR_ANNOUNCED`, and that root's git branch + HEAD against
`StateKey.GIT_HEAD_ANNOUNCED` (`<branch>@<short-hash>`, one bounded
`git rev-parse`; a non-repository root reads as "nothing to announce") — and
splices ONE notice immediately before the turn's HumanMessage per fact that
differs. Any number of switches between two messages coalesce into the single
notice naming the FINAL state, a switch back to the announced state sends
nothing, and the first turn of a session only records the baselines (silently).
Both values are mirrored durably and re-warmed at boot
(`prime_mem_from_store()` / `prime_git_head_from_store()`), so a restart cannot
lose them. A directory switch re-bases the git token in the same turn and words
its notice as "the working directory moved to X, whose repository is on Y"
(the old token belonged to another repository). The notices are
**HumanMessages** — the injected-carrier role — carrying
`metadata={"origin": "project_dir"|"git_head", "internal": True,
"provenance": "workspace_notice"}`: the message store keeps that origin on the
row (both row builders), and the chat renders a non-`user` origin as a neutral
system card (labels 项目目录切换 / 分支切换, folder and branch glyphs) instead of a
user bubble. Because they are human rows, the scanners that walk human messages
skip them through `pub.func.message.workspace_notice.is_workspace_notice`:
`split_into_turns` (a notice is not a turn boundary), HITL's
`_is_headless_turn` (a trailing notice does not make a turn operator-less), and
the summarizer's serializer (labelled `[System notice]` /
`system`, never `[User]`).

The read rule that keeps this working: **resolve the root per call, never at
construction time.** Tool objects are process-level singletons
(`agent/core.py`), so a captured root would freeze every session onto the first
caller's directory; and a session can switch at a turn boundary, so even a
per-turn cache would go stale after a HITL resume. The single accessor is
`agent.tools.pub_base.session_workspace_root(session_id)` feeding
`resolve_workspace_path(path, root)`; `PathGuard`, the four file tools,
`terminal`, `python_repl`, `ptc` and the code-intel runners all read it (the
guard also moves its boundary). In-root is NOT the end of the story: because an
unconfigured session's root IS the checkout, the four file tools also meet the
sherry-self read/write allow-lists (`self_path_policy.py`, see the security
section), which is what stops `patch_file` from rewriting `agent/core.py` and
`read_file` from reading `.models`.

Derivation: a subagent inherits the parent's binding at spawn
(`resolve_spawned_workspace_inheritance`) and the value is persisted as the
CHILD session's own binding, so a child is frozen to the root it started with
and does not follow a later parent switch. `sessions_spawn(cwd=...)` overrides
it. The prompt gains a `## Current Working Directory` block naming the effective
root, and marks an unbound session with 未绑定项目目录 instead of quietly
pointing the model at the sherry checkout.

**The root is a small project-level config surface.** `.sherry/` is the ONE
hidden entry sherry creates inside a project:

```
<project>/.sherry/
├── codeintel/index.db    # per-project code index (was .codeintel/)
└── shadow-git/           # per-project write snapshots
```

It is created LAZILY by `runtime/session/project_dir.py::maybe_create_sherry_dir`,
called from `WorkspaceNoticeMiddleware` before it records the announced baseline:
only when `project_dir_source(session_id) == "session"` (a process default never
counts), the root is the first announced one or differs from it, and `.sherry/`
does not exist. Binding alone creates nothing; the first MESSAGE does. It creates
the shell only — FACTS.md stays global, see below. An existing shell is never
recreated, and a legacy `.sherry/memory/FACTS.md` from the short-lived
per-project experiment is left untouched but is not read. The code index and
shadow git create their own subtrees on first use, so a missing `.sherry/` is
never an error. `CODE_INTEL_DIR` (the ast-grep binary cache under SHERRY_HOME)
and `.sherry-tmp-*.swp` (same-filesystem atomic replace) are deliberately NOT
part of this directory.

**Project instructions**: the builder in `workspace/prompt_builder.py` reads
`<root>/AGENTS.md` and falls back to `CLAUDE.md`. It is
registered as the `project_instructions` reminder source
(`agent/middlewares/reminder_catalog/sources.py`, priority 62 between continuity
65 and the directory notice 60, `budget_chars` 8 000, `only_if_changed=True`) —
never the cached system prompt, so a large file cannot invalidate the provider
prefix cache and a directory switch automatically re-reads the new project's
file. Only an EXPLICIT root contributes these files: a session binding or a
process default (`SHERRY_PROJECT_DIR` / `sherry.jsonc`) counts, while the
`project_dir_source == "default"` fallback to the sherry checkout injects
nothing — the checkout's own AGENTS.md is the agent's developer guide, never the
user's project rules. No nested traversal (git root → cwd) yet, no file creation,
no `.gitignore` edits.

**FACTS.md is ONE global, system-level file.** All three memory files live in
`workspace/memory/` (`MEMORY.md`, `USER.md`, `FACTS.md`) and are loaded by the
boot-time `MemoryStore.load_from_disk()`; there is no per-project FACTS path and
`runtime/session/context.py` no longer exists. FACTS holds only SYSTEM-LEVEL /
CROSS-PROJECT pitfalls — things true in ANY project on this machine (toolchain
behavior, environment limits, generic command gotchas), not project conventions
(those go to MEMORY.md or the project's AGENTS.md). The memory review and plan
extraction prompts state this routing and instruct the model to migrate a
project-specific entry out of FACTS into MEMORY when it spots one; the 1 375-char
oldest-first rollover still bounds the single file. Because nothing is
per-project, a directory switch no longer invalidates the cached system prompt
for FACTS, and `GET/PUT /memory` reads/writes all three files globally without a
`session_id`. A legacy `<project>/.sherry/memory/FACTS.md` is ignored (left on
disk, never rewritten) — it is not migrated automatically.

The external-path HITL gate agrees with that root: `resolve_external_path`'s
first check is "inside `ROOT_DIR` **or inside the session's project directory**"
(state lookup only on the non-repo branch, so the repo fast path stays
state-free). Selecting a project directory therefore exempts it and every child
path from "external file" approvals — including when a caller arrives with the
process default as its root (cold mem tier right after a restart) — and the
approval prompt names the session's root, not the checkout.

File browsing (`GET /project/tree`, `GET /project/file`,
`server/service/project_files_service.py`) is read-only and hard-refused to the
session's root: `resolve_within(base, path)` runs the same gate order as the
tools, reads go through `_open_no_follow` (TOCTOU), and `FILE_BROWSER` bounds
size (413), non-UTF-8 (415), depth and entries per level. Binary previews
(PDF / docx / image) ride the sibling `GET /project/file/blob` — the same
boundary and error protocol with the wider `max_blob_bytes` (20 MiB) cap and a
`mimetypes` Content-Type instead of the 415 no-UTF-8 rule — and the viewer tab
renders them client-side: pdf.js page-by-page onto one canvas (`PdfViewer`,
200-page cap, worker from the bundled `?url` asset), mammoth → HTML → the
`v-safe-html.fragment` DOMPurify path (`DocxViewer`), and images through an
object URL with the global click-to-zoom overlay; the format classifier
(`app/utils/file-format.ts`) is the one branch point and unknown suffixes
(legacy `.doc` included) degrade to plain text. It deliberately does
NOT use the agent's `resolve_external_path` HITL flow. The client shows the chip
(`ProjectDirectoryChip`), the lazy tree (`ProjectFileTree`) and the viewer tab
(`FileViewerPanel`, a SESSION-scoped tab: a file of the session's directory belongs
to that session, so it opens under 当前会话 like the commit diff). The tree stays
READ-ONLY but its rows are actionable (ZCode's workspace-file-tree parity): one
`<ContextMenu>` per tree (rows emit their right-click upward — a per-row popup
would mount one hidden menu per file) offers Open / Open with (viewer, plus the
system default app on the desktop build) / Open in the built-in browser (HTML
files; the page is served from the read-only endpoint as a blob URL, so relative
references inside it do not resolve) / Reveal in the file manager (desktop only,
`@tauri-apps/plugin-opener`; the label follows the platform, and the web build
hides the entry because a browser cannot reach the OS) / Copy absolute path /
Copy relative path / Add to chat. A row is also `draggable`, and a file-viewer
TAB is too: both publish a `text/x-sherry-file` JSON payload plus the plain-text
`@./path`, and the composer's drop handler turns either into a mention chip
(`use-file-mention.ts`: an atomic `<span contenteditable="false">` whose visible
label is the icon + file name while `data-file-path` holds the `@path` the send
extracts — `extractTextForSend` also feeds the draft model, so chips never leak
their label into a message). "Add to chat" travels the app-wide mitt bus
(`chat:file-mention` with the session id) from the left sidebar to the chat page,
which relays it to the composer it owns; the agent learns the convention from a
stable `## File References` block in the system prompt. The sidebar's 工作目录 body stacks TWO collapsible sections —
文件树 and the read-only **Git Graph** (`GET /git/graph`,
`server/service/git_graph_service.py`; `config/features/infra_side/git_graph.py`
bounds the page size, the served text and the git timeout): one
`git log --all --date-order` page (newest first, `limit`/`skip`, one extra commit
to report `has_more`; the panel appends the next page when the list is scrolled
near its end — there is no load-more button, and a page that fails offers a retry
in the same row) plus the current branch and a capped dirty count, with
refs parsed to `head`/`branch`/`tag`/`remote` chips. A directory that is not a
repository (or a host without git) answers `available: false` with a reason
instead of an error, so the panel renders its own empty state. `GitGraphPanel.vue`
lays the commits into lanes and draws one SVG per row (merges and HEAD get their
own node shapes), and it is the client's only consumer of that route. The two
sidebar section headers (文件树 / Git Graph) share one row.
The row's context menu offers 回退/切换 — `POST /git/reset` (`soft`/`mixed`/
`hard` only) and `POST /git/checkout`, both behind the client's confirm dialog,
with git's own stderr surfacing as a 409 — and clicking a row opens a DRAWER of
the files that commit touched directly under that row (an accordion, never a block
parked at the end of the list — a click elsewhere in that drawer replays the row's
own contextmenu, so the SAME action menu opens anchored at the row's right edge; `GET /git/commit`: metadata plus `--name-status -M -z`,
NUL-framed so a non-ASCII path is never quoted and the entry lookup behind the
diff still matches). Clicking one of those files opens its diff in a right-sidebar
tab (`GET /git/commit/file`, `GitDiffPanel.vue`, one tab per file): both sides come
from the commit's own blobs (`<parent>:<old_path>` / `<hash>:<path>`) and are aligned
by `difflib` opcodes, so a change reads as remove-left / add-right (red / green
tints), an added or deleted file is a SINGLE column, a rename follows its old path,
and a binary or oversized blob (2 MB / 4000-row caps) is flagged instead of decoded.
A path that is not part of the commit is a 404, never an empty diff. On the
client side every git call THROWS on failure (`bridge/git.ts`): the two write
actions go through the raw transport so a refusal reaches the toast with git's
own reason (a checkout blocked by a dirty tree names the files), and a failed
read rejects rather than resolving the shared client's null payload — a null
assigned to the panel's page threw on every later render, which left the panel
visibly dead (no row expansion, no branch switch) until a reload. The header's
branch label still renders the switcher dropdown the panel ships with, but
since user-git writes were replaced by per-project shadow checkpoints
(commit 99773730) the graph payload pins `branches: []`, so the dropdown stays
disabled — the panel's own empty-branches state. A checkout made through the
ref-chip menu (or in a terminal, or a commit/reset outside the session) still
reaches the AGENT as the git notice described under Project Directory Binding:
the next turn's transcript gets one `git_head` card before the human message.

The taskbar's **hammer button opens a three-entry toolbox** (浏览器 / 终端 /
白板) as the same dialog shape the settings menu uses. The browser and the
terminal open a right-sidebar tab in the SESSION group PER CLICK (a fresh
`instance` key in the tab payload — the strip numbers the twins 浏览器 1 /
浏览器 2, and each panel keeps its own history / scrollback under
`<sessionId>::<instance>`); the whiteboard is deliberately NOT a twin (one board
per session tab: a second click must show the same drawing), and the settings
entries keep deduping on their empty payload (`toolboxTools` in
`pages/home/config.ts`, wired through the same `HOME_TOOLBAR_EVENTS` registry as
the settings grid — a pinned test keeps the grids and the commands in step). The browser is an address bar over an iframe with
a per-session history (back / forward / reload; sites that refuse framing stay
blank, which the empty state says), a **free-size mode** that emulates a mobile
device viewport (resizable frame with edge/corner handles), and 打开调试工具,
which opens the page in a real window because an iframe cannot hand out devtools;
the terminal is a console for commands the
OPERATOR types: `GET /terminal/info` names the directory (the session's project
directory — the selected 工作目录 wins) and `POST /terminal/run` executes one line
through `/bin/sh -c` there with a scrubbed environment, bounded by
`TOOLS_TIMEOUTS["user_terminal_timeout_seconds"]` and an output cap, answering the
exit code instead of raising. It is a one-shot console, not a PTY — an interactive
full-screen program has nothing to attach to — and it is NOT the agent's
`terminal` tool (that one keeps its HITL gate and sandbox wrapper; this one is the
human's own shell, loopback-only via the gateway). **The whiteboard** (`WhiteboardPanel.vue`) is a freehand Canvas2D board:
`composables/use-whiteboard-canvas.ts` owns the drawing math (a stroke is a
polyline of points NORMALIZED to the fixed 1280×800 logical canvas, so CSS size
changes never alter the drawing; one point paints a dot; the eraser paints the
paper colour; the PNG export always renders at the logical size on an offscreen
canvas) and `stores/whiteboard.ts` owns the boards (in memory, keyed by the
tab's `boardId`, undo/redo as two stacks — a new stroke clears the redo stack —
plus a 5 000-stroke cap; closing the tab does not delete the board). The tab is
SESSION-scoped and opened at the wide 640px width; there are no text/shape tools
and no persistence (a reload starts empty). **加入对话** exports the board to a
PNG, strips the data-URL prefix and emits `chat:add-image` with
`{sessionId, payload: {base64, name}}` — the SAME mitt relay shape as
`chat:file-mention`; `[sid].vue` pushes it into the pending `selectedImages`
tray (its frozen `mySid` filter keeps KeepAlive-cached pages from stealing the
drawing, and it enforces the 10-image cap), and the send path then carries it
like any pasted image. The board must never build a `MultiModalMessage`
directly: `handleSend` re-reads the tray and would drop it. A browser without
Canvas2D shows a fallback line instead of crashing.

The browser panel's address bar
suggests the persistent VISIT HISTORY: every navigation (iframe mode from
`stores/toolbox.ts::navigate`, CDP mode from the `page` frame the server reports —
the devtools frontend is excluded) records one row in the Dexie `browserVisits`
table (`composables/use-browser-history.ts`, schema version 3), so the list
survives a reload and an app restart. A repeat visit inside 30 s UPDATEs the
newest row instead of appending (CDP reports several frames per navigation), the
table is capped at 500 rows (oldest pruned), and the dropdown
(`BrowserHistoryDropdown.vue`) matches URL or title case-insensitively, newest
first, showing a favicon (hidden when it 404s), the relative age in the active
language and a 当前会话 mark, with a clear action for the whole history.
Both panels keep their state in
`stores/toolbox.ts` because the sidebar unmounts an inactive panel.

Choosing a directory is one action with two runtimes: the desktop build (Tauri)
opens the OS folder dialog (`app/utils/project-directory.ts` →
`@tauri-apps/plugin-dialog`), the browser build opens the in-app picker
(`ProjectDirectoryPicker.vue`) — a browser cannot hand out an absolute path, so
there is no typing channel. That picker is the one deliberate exception to the
in-root rule (`GET /system/dirs`, `server/service/system_folders_service.py`):
it lists the direct **subdirectories** of one absolute path (names only, no
files, no contents; `FILE_BROWSER` caps the level) and sits behind the same
gateway auth/CORS/CSRF middleware as every other route. The toolbar's files
button (`pages/home/index.vue`) exists only while a session is open AND its
project directory is bound — the tree it opens is scoped to both, so an unbound
session shows no control at all (nothing to press, no empty picker) — and the
persisted sidebar body falls back to the session list whenever the button goes
away (last session closed, binding cleared).

## Packaged Desktop App (Tauri) (`client/src-tauri/`)

A release is a UNIFIED installer (packing-plan, Electron-style): `pnpm tauri
build` (client/) bundles the static SPA, the Rust shell AND the CPU backend tree
(`bundle.resources = ["sherry-backend/"]`, unpacked into the resource dir by the
desktop workflow before the build) — first launch runs with zero downloads.
`packaging/build_backend.py` still produces the per-platform backend archive as
a GitHub release asset, now the DOWNLOAD FALLBACK (an older installer, a
stripped install, a resource mismatch), and `packaging/build_cuda_pack.py`
produces the optional Linux-only CUDA pack asset the user opts into from the
model settings tab. `.github/workflows/desktop-build.yml` builds all of it on
ubuntu/windows/macos (artifacts for a push to `main`, a GitHub Release for a
`v*` tag): `build-tauri` needs `build-backend` (and `build-cuda-pack` runs
beside them), so the installer carries its own backend.

Release hygiene is a build gate: `packaging/check_release.py` rejects the repo's
`tests/`, `evals/`, `wiki/` and `wiki/` trees (plus frontend `*.test.*` /
`*.spec.*` files) in BOTH halves, and `build_backend.py` runs the backend half
itself before the tree is compressed. `.dockerignore` applies the same boundary
to the server image. The wiki/wiki trees are also outside the pre-commit hooks
and the code CI jobs (docs parity/link gates run in their own job); see
`.github/workflows/ci.yml`.

- **Frontend**: `nuxt generate` (`pnpm build:desktop`) writes `.output/public`
  — the `frontendDist` a bundled app can serve from disk (the `nuxt build`
  output needs a Nitro server and is NOT it).
- **Backend build**: `packaging/build_backend.py` runs PyInstaller **onedir**
  into `client/src-tauri/sherry-backend/` (per-OS; PyInstaller cannot
  cross-compile), writes the `VERSION` marker (the `tauri.conf.json` version —
  what `package_info()` reports and the value the shell compares) and the
  archive is built with the tree's CONTENTS at its root. The entry
  (`packaging/backend_entry.py`) imports `server.__main__` STATICALLY and calls
  its `main()` — a `runpy.run_module` is invisible to the freezer, which is how
  the first build shipped without the `server` package at all. The Linux job
  swaps PyPI's CUDA-pulling `torch` for PyTorch's CPU-only wheel (~5 GiB saved
  — the difference between fitting the asset cap and not); macOS/Windows wheels
  are already CPU-only.
- **Which backend runs**: the shell prefers the BUNDLED tree —
  `resource_dir()/sherry-backend/sherry-backend` present _and_ its `VERSION`
  equal to this build's → spawn it straight away (`"Using the bundled Python
  backend (no download)"`). Otherwise `services/backend_downloader.rs` owns the
  fallback: download `sherry-backend-<os>-<arch>.tar.gz|.zip` from the latest
  release (`EMA_BACKEND_URL_BASE` overrides the base URL) on a background
  thread, unpack into a staging dir with the system `tar`, swap it in, then
  spawn it. Tauri events (`backend-download-started` / `-progress` /
  `-complete` / `-error`, plus the `backend_download_status` pull and the
  `retry_backend_download` command; `use-backend-download.ts` +
  `BackendDownloadOverlay.vue` render it, and the SPA reloads once the backend
  is up). The whole path is gated on `cfg!(dev)` being FALSE: a dev build
  (`pnpm tauri dev`, a plain `cargo build`) neither ships nor downloads a
  frozen backend, and spawns `python -m server` from the checkout when
  `EMA_AUTO_START_BACKEND` asks for it.
- **Optional CUDA pack** (Linux only): `services/cuda_detect.rs` probes
  `nvidia-smi` / the kernel-module record and gates the model settings tab's
  card (`cuda_support` command; `CudaPackCard.vue` + `use-cuda-pack.ts` +
  four-locale `config.cuda.*` copy). The opt-in download
  (`cuda_pack_downloader.rs`, events `cuda-pack-download-*`) resumes via HTTP
  Range (206 continues, 200 restarts, 416 resets an over-long tmp), installs
  into `<app_home>/sherry-cuda-pack/` with the same staging + swap, and its
  `VERSION` records the backend version it was built from (one pipeline builds
  both — a matching backend version transitively pins the torch ABI). At every
  packaged start the process manager re-checks pack vs backend versions and, on
  a match, joins the pack's `*.so` directories to the loader via
  `ld-linux --library-path` (probed loader path, NOT `LD_LIBRARY_PATH`); the
  `restart_backend` command makes a fresh download take effect (torch resolves
  the CUDA libraries at import). A pack never touches the env, and an
  uninstall falls back to CPU on the next start.
- **Never symlink the backend tree**: `tauri-build`'s build-time resource copy
  (the `bundle.resources` entry the unified installer restored) uses
  `std::fs::copy`, which truncates the destination before reading the source —
  a `target/release/sherry-backend` symlink pointing back at the source made
  that copy empty every file (observed: 7,634 zero-byte files in one build).
  `packaging/build_backend.py` removes a leftover symlink of that name before
  building, which is what makes the resource entry safe again.
- **The app home is the writable root** (`config/path.py`): the shell passes
  `--sherry-home <OS app-data dir>` on the backend's command line (the
  `SHERRY_HOME` env var remains a transitional fallback), and every writable
  path derives from it — `src/`,
  `workspace/` (sessions, memory), `temp/`, `logs/`, `models/`, `.codeintel/`,
  `.env`, `.models` / `.network` (gitignored; seeded from their templates),
  `sherry.jsonc`, and the WRITABLE half of the skills tree
  (`USER_SKILLS_DIR`, i.e. `auto/` + `plugins/`, with the loader scanning both
  it and the shipped `skills/builtin`). The downloaded backend tree lives there
  too — the installed app is read-only and is replaced by updates. Unset (a
  checkout, a server, CI) every
  path stays exactly where it was, so nothing changes for existing installs; a
  first run seeds `.env` (from the tracked file) / `.models` / `.network` (from
  their `.example` templates) and `sherry.jsonc`. Locations
  reported to the UI keep the CHECKOUT shape (`./skills/auto/…`) whichever home
  they live in (`app_relative` / `resolve_app_path` map between the two).
- **First run without a model**: the boot's 200K token gate aborts a SERVER,
  but the packaged shell passes the bare `--sherry-desktop` flag (packaged
  only; `SHERRY_DESKTOP=1` is the transitional env fallback), which lets an
  UNCONFIGURED desktop app serve anyway, so the window can open and 模型配置
  can write the files; a CONFIGURED-but-invalid value still aborts. The
  backend must be restarted (relaunch the app) after configuring — the runtime
  reads the file at boot.

## MCP Servers (`agent/tools/mcp_plugin.py`)

MCP servers come from TWO layers merged by name (user overrides bundled):
the read-only bundled defaults (`plugins/mcp_server/config.json`, shipped in
the installer) and the user drop folder `APP_HOME/mcp_servers/` — the ONE
writable place a user adds MCP servers in a unified install (`plugins/` is
read-only there; the folder is seeded with a README on first boot of a
desktop home, never in a checkout). Two drop shapes are scanned: a flat
`<name>.json` (the file IS the server spec) and a self-contained
`<name>/config.json` directory whose `$here/` resolves to the directory
itself, so `server.py` travels beside its config; the bundled file's
`$here/` keeps resolving to `plugins/mcp_server/` (legacy behavior). Every
entry parses in ISOLATION — a bad JSON file shows as a ⚠ entry in
`GET /mcp/servers` (`server/service/mcp_service.py`, the extend panel's MCP
tab renders it) and is skipped by the loader instead of failing the load;
noise (non-JSON files, directories without a config) never appears.
`build_mcp_tools()` connects one `MultiServerMCPClient` PER server, so a
dead server costs only its own tools, and the tool names
(`mcp__<server>__<tool>`) still file under the catalog's `mcp` group
unchanged. A config change takes effect on the next backend restart (the
agent graph holds the tool list; the extend panel's restart button rides
the same `restart_backend` command the CUDA pack uses). Only drop trusted
MCP servers — their commands run on this machine.

## Configuration Files (`.env` / `.models` / `.network`)

The backend's user configuration is split by sensitivity into three project-root
files (`APP_HOME`, so a packaged app keeps them in its app-data dir):

- **`.env`** — TRACKED in git. Non-secret application configuration only
  (`SANDBOX_*`, `SHERRY_GATEWAY_*`, `SHERRY_AUTH_*`). Never put a key here.
- **`.models`** (gitignored; template `.models.example`) — the model families:
  provider / endpoint / key / local-model switch for main, auxiliary, TaskFlow
  decisions, compression, ITTT, VTTT, TTI, reranker, embedding and STT, plus
  the scanner's LLM selection. Loaded by
  `config/models_settings.py`.
- **`.network`** (gitignored; template `.network.example`) — network services:
  `TAVILY_API_KEY` + the `SEARXNG_*` backends, the `SHERRY_BROWSER_*` switches
  and `LANGSMITH_*` tracing (moved out of `sherry.jsonc`, which is tracked and
  must not hold a key). Loaded by `config/network_settings.py`.

Both files are JSON5 (flat `"KEY": value,` lines; comments and trailing commas
allowed). The loaders SEED a missing file from its template, overlaying the
values still found in a legacy `.env` (and, for the tracing keys, in
sherry.jsonc's old `LANGSMITH` object), then `hydrate` every key into
`os.environ` so the existing readers (`os.getenv`, the import-time feature
snapshots, subprocess scrubbing) are unchanged. Precedence is **explicit process
variable → `.models` / `.network` → legacy `.env` → registry default**: the
hydration skips an already-set key, and `config.path` hydrates BEFORE it
(re)loads the environment file — `config.path.load_env()` is the single reload
seam (bare `load_dotenv(..., override=True)` calls were replaced by it).

Two OPTIONAL internal families are configured here too, both system-level (they
are deliberately NOT part of the per-session agent config):

- `TASKFLOW_LLM_*` — the TaskFlow decision model. It serves the four internal
  calls `taskflow_flow(action="plan")` / `taskflow_flow(action="replan")` / `step_judge` / `taskflow_flow(action="finish")`
  (temperature 0). Non-taskflow callers stay on auxiliary: the subagent
  completion judge, the spawn/steer branches and plan extraction.
- `COMPRESSION_LLM_*` — the summarization middleware's own model: the LLM
  summary (`_create_summary` / `_acreate_summary`). It does NOT serve memory
  review (the MAIN model, cache-warm by design) or plan extraction (the
  auxiliary model).

Both are built by `models/LLMs/family_llm.py::build_family_llm` (exposed as
`build_taskflow_llm` / `build_compression_llm`): an empty provider + api_base
falls back to `build_auxiliary_llm()` (an existing deployment is unchanged), a
remote configuration builds the same client shape as auxiliary (bounded
timeout / retries / per-provider max-output), and `*_MODEL_LOCAL=true` reuses
the auxiliary GGUF — there is no second local model to download. A configured
`*_MAX_TOKEN` must be >= 200000 (200K) and `*_MAX_OUTPUT_TOKEN` empty or
>= 8192 (8K): the save path refuses a smaller value and the boot/runtime gates
abort, exactly like the main/auxiliary keys. The 模型配置 tab renders both
families as model-profile cards, whose local entry reports the aux family's
download progress; a pre-existing `.models` gains a wholly missing family block
on import (`ensure_models_config_file`), because the panel only renders keys the
file contains. A configured-but-unbuildable remote model (unknown provider)
logs a warning and falls back to auxiliary instead of stranding TaskFlow or
compression. An existing `.models` that still carries the previous `131072`
default is migrated to `200000` when the file is loaded
(`models_settings._ensure_context_window_floor`); any other below-floor value is
left for the boot gate to reject.

Drift is refused, not merged: `server/service/env.py` treats the moved keys as
`SPLIT_OUT_KEYS` (the two registries are the source of truth) — a legacy `.env`
line is neither shown by `GET /env` nor accepted by `PUT /env` (the error names
the file to edit). `GET/PUT /models-config` and `GET/PUT /network-config`
(mirroring `/sherry-config`) are the writers; the 模型配置 tab merges the three
sources and routes each group's save back to its own endpoint, while the
模型联网 tab writes Tavily / SearXNG through `PUT /network-config`.
`.env` no longer gets a `.env.bak` (it is tracked — git is the backup);
`.models` / `.network` keep one.

The 模型配置 panel's built-in local entry (embedding, reranker, ITTT / VTTT,
auxiliary) prefetches the same GGUFs the runtime would download lazily, with a
live progress bar. `config/features/infra_side/model_downloads.py` (`MODEL_DOWNLOADS`)
lists each family's files and their `model_weight` target; `models/downloader.py`
does the HEAD size probe plus a streaming `.part` write and atomic replace;
`server/service/model_downloads_service.py` runs one background worker per
family (HEAD/no-Content-Length files report bytes rather than a bogus percent,
an ITTT/VTTT GGUF already under `auxiliary_llm` is copied locally instead of
re-downloaded) and `GET /model-downloads/status` / `POST /model-downloads/download`
(family in the body) expose it. Live progress is PUSHED, not polled:
`server/service/model_download_events.py` sends `model_download_updated` through
`runtime/session/ws_push.py::broadcast_to_all` to every live socket (a download
is process-level, not session state), throttled per family by
`MODEL_DOWNLOADS["progress_push_interval_s"]` (0.25 s) with state transitions
forced, and the worker thread schedules onto the loop captured by the start
handler. The panel takes ONE status snapshot when opened or when a socket
reconnects, then applies frames; there is no polling interval. The code
embedder publishes the same event with `kind: "code_embedder"` and keeps its own
`/code-embedder/*` snapshot pair.

## Compression Thresholds, Microcompact & Output Caps (ZCode/PI parity)

- **Per-model output caps** — every chat family carries its own
  `*_MAX_OUTPUT_TOKEN` in `.models` (main / auxiliary / TaskFlow / compression)
  beside the context-window keys. Empty means "do not send a field" (provider
  default); a set value must be >= 8192 (8K) and is refused at boot, at
  `built_agent()`/spawn validation, at model build and at the
  `/models-config` save (M9). Each cap must also be SMALLER than its family's
  effective context window (an optional family with an empty own window falls
  back to the auxiliary window or the remote default). `/models-config` rejects
  `output >= window`, and the
  模型配置 input blocks the same comparison against the draft's sibling
  `*_MAX_TOKEN`. The panel labels it 最大输出窗口 and every
  model category title carries a help mark describing its purpose/use cases
  (four languages). `models/LLMs/provider_max_output.py` maps the
  provider to the kwarg `init_chat_model`'s class accepts — `max_tokens` for
  OpenAI-compatible providers, Azure and Anthropic; `max_output_tokens` for
  Google Generative AI / Vertex; an unknown gateway gets no field (M4). When
  thinking is enabled the budget headroom is based on the configured cap, not
  the legacy `MAIN_LLM_OUTPUT_MAX_TOKEN`.
- **Sampling-time repetition penalties** — every chat family carries a
  `*_FREQUENCY_PENALTY` / `*_PRESENCE_PENALTY` pair in `.models` (main,
  auxiliary, TaskFlow, compression). Empty uses the global
  `REPETITION_PENALTY` defaults (0.3 / 0.2); a set value must be a finite number
  in [-2, 2] and is refused by `/models-config` otherwise.
  `models/LLMs/provider_repetition_penalty.py` maps it per provider:
  OpenAI-compatible and Google get both fields, Ollama additionally gets its
  native `repeat_penalty` (1.1), and Anthropic / Bedrock / an OpenAI reasoning
  model (o-series / gpt-5, whose API rejects the parameter) / an unknown
  gateway get none (M4). The 模型配置 panel renders each pair as a number input that
  rejects non-numeric keystrokes and clamps to the range; this is preventive,
  and `OutputRepetitionGuard` remains the reactive backstop.
- **Compression line** — `agent/middlewares/summarization/thresholds.py` is the
  single source: with `MAIN_LLM_MAX_OUTPUT_TOKEN` set, the line is
  `context_window - max_output` (the full input budget); unset keeps the legacy
  80% behavior (the overflow router keeps its budget-relative band when no cap
  is set, so existing timing is unchanged). `GET /context_usage` returns
  `max_output`, `compress_tokens` and `compress_ratio = compress_tokens/window`;
  the ring/dashed line/percent label use that ratio, never a local 0.8. A cap
  equal to or larger than the window is refused by `/models-config`
  (`max_output < MAIN_LLM_MAX_TOKEN`) and clamps to a 0% line at runtime.
  `CompressionMixin._effective_context_window()` remains
  `ctx - min(max_output, ctx)` for preserve/overflow budgets; `_preemptive_check`
  compares absolute thresholds and the soft truncate line scales with the hard
  line (0.70/0.80) only when a cap is set.
- **Microcompact** — `agent/middlewares/summarization/microcompact.py` runs at
  the end of `_run_non_llm_strategies`: on a 60-minute idle (process-local
  completion clock) or at `min(auto_threshold * 0.9, auto_threshold - 2000)` it
  replaces OLD tool results with `[Old tool result content cleared]` (newest 5
  kept, media/already-cleared skipped, min 256-token savings). No LLM call.
- **User-requested compaction** — the `compact_now` main tool (memory catalogue
  group) queues `StateKey.SUMMARIZATION_COMPACT_REQUESTED`; the next model
  call's T2 dispatch runs one compaction with trigger `USER`, bypasses the
  anti-thrash gate and records a `{trigger: user_requested, phase: standalone_turn}`
  trajectory event.
- **ModelDownshift** — a session model profile may carry an optional
  `context_window` (decimal string; client descriptor + backend sanitizer).
  `_session_context_window(session_id)` feeds it into the preserve/overflow
  budgets, so a switch to a smaller window compacts at the next pre-request
  check and the compact event carries `reason=model_downshift`. An absent field
  keeps the env window. It is a threshold effect, not a switch-time hook.
- **Compact phase/reason** — `phase.py` derives `CompactPhase` (standalone_turn /
  pre_request / post_response / reactive / mid_turn) and `CompactReason`
  (context_limit / provider_overflow / user_requested / model_downshift) from the
  existing trigger string; every compact path emits one trajectory event with
  `phase`, `reason`, the unchanged `trigger`, `messages_before`, `messages_after`
  and `route`. T1/T2/T3/user compactions share `_finish_compact`; T4/T5 record
  from `_finish_forced_recovery`.

## Provider Prompt Caching (`models/LLMs/anthropic_cache.py`)

Anthropic is the one supported provider whose prompt cache is OPT-IN: nothing is cached unless the request marks a
prefix with `cache_control`. Everything above (the frozen system prompt, the session-stable tool face, live state
pushed down into the reminder layer) was built for providers that cache automatically, so on Anthropic it earned
nothing. A `BaseChatModel` wrapper mounted at every client build point — `main_llm._build_inner_chat_model_from`,
`auxiliary_llm/core.py` and `family_llm.build_family_llm` — rewrites each outgoing request before delegating.

- **Three breakpoints**, in Anthropic's prefix order (tools → system → messages): BP1 the last text block of the
  `SystemMessage` (also caches the tool list, since system follows tools), BP2 the last converted tool definition
  (only pays off when a thinking switch or an agent-config edit changes the system prompt while the tools stay put),
  BP3 the last real history message found when scanning back past the current turn's human carriers. BP3 anchors the
  PREVIOUS turn's settled tail on purpose: `DynamicReminderMiddleware` and the workspace notice splice human messages
  (a reminder, then the new user text) whose content changes per turn, so a breakpoint on the last message would miss
  every time. The fourth slot stays free.
- **Config** — `ANTHROPIC_CACHE` (`config/features/agent_side/anthropic_cache.py`): `enabled` (`SHERRY_ANTHROPIC_CACHE`,
  default on), `ttl` (`SHERRY_ANTHROPIC_CACHE_TTL`: `short` = `{"type": "ephemeral"}`, `long` adds `"ttl": "1h"`),
  `max_breakpoints` (clamped to Anthropic's limit of 4). Breakpoints the caller already placed count against the
  budget; nothing is added twice.
- **Gating** — `anthropic` only. Bedrock's Claude goes through `langchain-aws` (different cache API) and a
  Claude-routed OpenRouter request loses the marker in the OpenAI-shaped client, so neither is claimed. Every other
  provider's request is byte-identical to before.
- **Cost** — `MODEL_PRICING_TABLE` entries take optional `cache_read` / `cache_write` prices (USD per 1M cached
  tokens); `TokenBudget._cost_delta` splits `usage_metadata.input_token_details` into uncached / cache-read /
  cache-write and charges each rate. A table without the two fields prices cached tokens at the plain input rate (the
  pre-existing estimate — no invented discount). OpenAI-shaped usage reports `input_tokens` INCLUDING the cached
  ones, so they are subtracted before the uncached rate applies; Anthropic's `input_tokens` already excludes them.
- **Fail-open and non-mutating** — an injection failure, an unconvertible tool list, or `enabled=false` sends the
  request unchanged; the caller's messages and tools are copied, never mutated (the middleware chain's state and the
  message store are untouched).
- **Semantics checklist** (why the existing invalidation points stay correct): a thinking switch reloads the memory
  snapshot and deletes `SYSTEM_PROMPT` → BP1 misses because the bytes changed; an agent-config edit invalidates BP1
  and BP2 together; a compaction rewrites prompt and messages → BP1 and BP3 miss; a changing reminder never touches
  BP3.

## LLM Retry, Backoff & Retry Events (ZCode parity)

`agent/middlewares/llm_retry/core.py::LLMRetryMiddleware` is the ONE retry layer
for main/fallback model calls: `LLM_CLIENT_DEFAULTS["main_max_retries"]` and
`["fallback_max_retries"]` are 0, so the provider SDK never adds a hidden second
budget. `config/features/agent_side/llm_retry.py::LLM_RETRY` defaults:
`max_retries=10` (11 total attempts), `base_delay=2.0`, `backoff_factor=2.0`,
`max_delay=60.0`, `jitter_mode="full"` (0.5–1.0×), `respect_retry_after=True`,
`retry_after_cap_s=300`, `empty_completion_retries=1`, `emit_retry_events=True`,
`max_fallback_switches=1`, `stale_giveup_scope="turn"`,
`stale_giveup_threshold=5`. `pub/func/retry_policy.py` owns the delay math and
`Retry-After` parsing (`retry-after-ms`, seconds, HTTP-date; read off the SDK
exception's `headers`/`response.headers` and carried by
`ClassifiedError.retry_after_s`).

**Counting model (ZCode parity).** Every `wrap_model_call` / `awrap_model_call`
starts a fresh local budget (`retry_count = 0`), so a new model call is a new
11-attempt request budget. The event distinguishes `attempt` (PHYSICAL request
number, would include extra attempts), `budget_attempt` (`attempt` minus
`extra_attempts`), `retries` (`budget_attempt - 1`) and
`max_attempts = 1 + max_retries + extra_attempts`. Only a new request path
resets the budget: fallback activation, multimodal skill degradation and a
content-filter rebind. Empty completion CONSUMES one ordinary retry (and is
separately capped by `empty_completion_retries`); auth-refresh / signature-repair
extra attempts (not implemented — no provider seam) would use subtraction, not a
reset. `before_agent` resets the per-turn `llm_fallback_switches` counter and,
when `stale_giveup_scope="turn"`, the cross-call `llm_stale_streak`; the breaker
still aborts the current turn at 5 consecutive timeouts, but a later turn gets a
fresh chance instead of being wedged forever. `max_fallback_switches` caps model
switches per turn (0 disables fallback, including a sticky candidate); the
session-level sticky `llm_fallback_index` is unchanged.

The loop re-calls on classified retryable failures, emits one
`model_retry` event per scheduled/succeeded/exhausted transition, and keeps the
existing fallback / content-filter / partial-stream-stub / stale-streak behavior.
A completion with no text, no tool calls and no usage retries once
(`empty_completion_retries`). The retry event is recorded in the trajectory
ledger (`runtime.hooks.RECORD_TRAJECTORY_EVENT`, kind `model_retry`; the event
id keys each transition by attempt + status, so `scheduled` and its
`succeeded`/`exhausted` outcome stay readable as separate rows) and pushed
to the session's sockets (`retry_events.py` → `broadcast_to_session`) as:

```text
{"event":"model_retry","session_id":…,"content":{
  "status":"scheduled"|"succeeded"|"exhausted",
  "attempt":2,"budget_attempt":2,"max_attempts":11,"retries":1,
  "extra_attempts":0,"fallback_switches":0,
  "reason":"rate_limit","delay_ms":1000}}
```

`attempt` is physical; `budget_attempt` / `retries` exclude the extra attempts;
`fallback_switches` is the per-turn count. An exhausted empty completion reports
its own attempt (e.g. `2/11` after one retry), not the 11th attempt.

The client maps both WS stacks (`composables/ws.ts` and
`agent-socket-frames.ts`) to `ws:model_retry`; `use-chat-stream` decorates the
live AI row's `retryStatus`, and `ChatBox.vue` renders a compact localized
counter (`chatBox.modelRetryScheduled/Succeeded/Exhausted`) in the message row.
`use-chat-turn-groups` keeps an EMPTY AI placeholder when it carries
`retryStatus` — otherwise the pre-first-token retry row would be filtered out.
`retryStatus` is in-memory only (a history reload drops it); the trajectory
event remains the durable record.

The session-push socket's disconnect path (`server/trigger/core.py`) clears the
session registers only when NO socket for that session remains: closing a core
socket while the agent-stream socket (or another viewer) is alive must not
unregister it from broadcasts or wipe session state mid-turn.

## Tool Parallel Safety (`agent/safe_tool_node.py`)

LangGraph's ToolNode gathers **every** tool call in one model batch. Sherry
replaces it in-process with `SafeToolNode` (patched into
`langchain.agents.factory.ToolNode` once at `agent/core.py` import, gated by
`TOOL_PARALLEL["enabled"]`) so a batch is scheduled by resource, not blindly:

- **Classification** (`agent/tools/parallel_safety.py`): only explicit
  `READ_ONLY_TOOLS` / `concurrent_safe` declarations are safe; unknown tools
  and missing metadata default to unsafe. `idempotent` is NOT used for this
  decision (`memory` is idempotent but writes session state).
- **File calls** (`read_file` / `write_file` / `patch_file`) group by canonical
  `file_path`. A group containing a write runs in the model's `tool_calls`
  order; an all-read group may run concurrently; different paths never wait.
  A path that cannot be resolved makes the batch a barrier.
- **Every non-file unsafe tool is a whole-batch barrier** (`terminal`,
  `python_repl`, `execute_code`, `ptc`, `memory`, session writes): its resource
  cannot be inferred from arguments.
- **Ordering**: within a batch, read-before-write reads the old content and the
  write then sees the read's license; write-before-read writes first, so the
  read sees the new content (and an unlicensed overwrite is refused by the
  existing rule). Cross-agent/cross-batch order is the file-lock acquisition
  order, not the model's call order.
- **File locks** (`agent/tools/pub_base/path_lock.py` / `file_lock.py`):
  `path_read_lock` is shared, `path_write_lock` is exclusive (writer
  preferred); `flock` uses `LOCK_SH` / `LOCK_EX`. `read_file` takes
  `file_read_lock`, the two writers keep `file_write_lock`. The batch scheduler
  orders same-batch conflicts; these locks protect concurrent agents and
  processes.
- **Resource middleware** (`agent/middlewares/tool_resource_lock.py`, required
  in BOTH the main and subagent chains): `side_effect_scope: "system"` takes a
  process-wide lock (`TOOL_PARALLEL["system_lock"]`, default OFF to avoid
  serializing all shell work); `"user_interaction"` takes a per-session lock
  (`interaction_lock`, default ON). `workspace` / `session` / `network` are
  deliberately not re-locked: file locks, memory, SQLite and the browser
  manager already own those resources.
- **Applies to every agent**: the factory patch is process-wide, so main,
  subagent and auxiliary `create_agent` graphs all use `SafeToolNode`; the
  subagent middleware chain carries the same `ToolResourceLockMiddleware`.
- **Rollout / compatibility**: `TOOL_PARALLEL["enforce"] = False` logs the plan
  but keeps native concurrency; `True` applies it. Tests pin the LangGraph
  private methods `SafeToolNode` depends on, so an upgrade fails loudly.
  `terminal`/`python_repl` still cannot be path-keyed; they remain barriers,
  and `system_lock` only adds cross-agent exclusion when explicitly enabled.

## Plan-Then-Execute Plan Mode (`agent/tools/plan_mode/`)

A session can DESIGN before it executes: the agent enters plan mode, works with
read-only tools, submits a plan and waits for the user's approval before a
single file is written. Two tools, `enter_plan_mode(reason, replan=False)` and
`exit_plan_mode(plan)`, drive a small state machine stored in BOTH register
tiers (mem for the hot path, DB so a restart keeps the mode) under
`StateKey.PLAN_STATUS` — `needs_planning` / `planning` / `ready` /
`needs_replan` — plus `PLAN_REVISION`, `PLAN_MUTATION_COUNT` and
`PLAN_FILE_PATH`. The tunables live in
`config/features/agent_side/plan_mode.py` (`PLAN_MODE`).

- **The read-only guarantee** is `ToolSelectionMiddleware`, doubled exactly like
  its per-session selection: `wrap_model_call` narrows `request.tools` to
  `PLAN_MODE["allowed_tools"]` while the status is `planning`/`needs_replan`
  (still bounded by the session's own set, except that `enter_plan_mode` /
  `exit_plan_mode` always stay reachable — a mode the model cannot leave would
  be a trap), and `wrap_tool_call` REFUSES a write tool that still gets called
  (“Plan mode is active…”). `sessions_spawn` is deliberately NOT on the
  whitelist: a child runs with its own session's state, so a spawned executor
  could write before approval.
- **The human can pin it too**: the toolbar's 访问模式 control gained a 计划模式
  entry (`PUT /sessions/access_mode` `mode=plan`) which sets the same
  `PLAN_STATUS`; the mode it reports is DERIVED from the plan status, so a plan
  the agent entered by itself shows up in the control as well, and switching to
  any other mode resets the status (leaving 计划模式 cancels the flow).
- **The prompt** carries it too: `workspace/prompt_builder.py` appends
  `_build_plan_mode_block()` on every call (never frozen, so entering/leaving
  takes effect immediately and a compression cannot lose it) — the four-phase
  workflow + “a turn may ONLY end with a `question` or an `exit_plan_mode`
  call” while active, the approved plan's file path while `ready`.
- **`exit_plan_mode`** persists the plan to
  `SESSIONS_DIR/<session_id>/plans/plan-<revision>.md` (revision =
  `sha256(plan)[:16]`, so the re-execution after a resume writes the same file)
  and suspends the graph with a HITL `interrupt` (the `question` tool's
  contract): approve → `ready` and the write tools return; reject WITH feedback
  → back to `planning` with the feedback (mutation budget
  `PLAN_MODE["max_mutations"]`, then the tool stops resubmitting); reject
  without a message → `needs_planning`.
- **Guards**: the revision binding (a stored revision or the plan FILE changing
  during the approval discards it), the size cap
  (`PLAN_MODE["max_plan_chars"]`, refused, never truncated) and the mutation
  budget. `PLAN_MODE["enabled"] = False` unregisters both tools and nothing
  else changes. The client renders the plan as an approval card in the input
  slot ([sid].vue, `isPlanHitl`) with the plan text, a feedback field, 退回修改
  and 批准 (the reject+message path is the “request changes” decision).
- Chain membership and order are unchanged (no new middleware); the tools join
  `_MAIN_TOOLS_BUILDERS` and the `plan_mode` catalogue group, so the 预设-工具
  tab can switch them per session. Tests:
  `tests/agent/tools/plan_mode/` + the plan-mode cases in
  `tests/agent/middlewares/test_tool_selection.py`.

## Per-Session Agent Config (`server/service/agent_config_service.py`)

The 预设角色 panel (the 菜单 entry renamed from 预设; it sits immediately after the 系统配置 entry, which leads the nine-grid, and the right
sidebar's tab label — global and 当前会话 alike — carries the same name) covers one JSON payload
per session in its last four tabs (工具 / 中间件 / 代理模型 / 技能), stored under
`StateKey.AGENT_CONFIG` (parked twin `AGENT_CONFIG_PENDING`, promoted at the turn boundary like
the model/thinking controls)::

    {"tools": [...]|null, "middlewares_disabled": [...],
     "subagent_models": {"<role>": {profile}|null}, "skills": [...]|null,
     "middleware_options": {"Summarization": {"nudge": bool}}}

- `GET /agent/catalog` is the client's only source of tool / middleware / role / skill names
  (`agent/tools/catalog.py::TOOL_GROUPS`, `agent/middlewares/catalog.py::MIDDLEWARE_ORDER`
  - `scaffolding.MAIN_REQUIRED_NAMES`, `roles/loader.py`, `skills/loader.py::skills_catalog`);
    the skill list carries the ACTIVE skills visible to main plus the 内置 / 第三方 split (`builtin:
false` = under `skills/plugins/`); each tool entry also carries the
    tool's own first description line (bounded) — the 工具 tabs show it as a hover tooltip, so
    it always matches the code (runtime notes like web_search's missing key included). `GET|PUT /sessions/agent_config`
    reads/writes the payload; the service rejects an unknown tool / skill / role, and any
    system-required middleware name, with a 400 that names it.
- **Tools** are applied per call by `ToolSelectionMiddleware`
  (`agent/middlewares/tool_selection/`): `request.override(tools=<enabled subset>)` narrows
  what the model sees, and `wrap_tool_call` refuses a disabled tool at EXECUTION (a stale
  checkpoint or a hallucinated name would otherwise still run — the ToolNode holds the full
  list). Unset config = every tool. `agent/tools/catalog.py` carries the three rules the
  payload obeys: `TOOL_ORDER` (group render order — 技能 / 终端与代码 / 文件读写 / 交互 first,
  memory fifth, then the task and delegation surfaces), `REQUIRED_TOOLS` (the file tools, code
  execution, the three skill tools, `question` and `message_search` — the 工具 tab shows those
  rows as locked chips with NO checkbox, the service 400s on a payload that omits one, and
  `enabled_tool_names()` unions
  them back in so an older register value cannot strip them either), and `BULK_ONLY_GROUPS`
  (`tasks` and `subagents` move as a whole in the UI — select-all / clear-all only, never a
  per-tool switch).
- **Skills** (`AGENT_CONFIG["skills"]`): the names whose `<available_skills>` index entries enter
  the system prompt — `null`/absent = every skill (today's behaviour), an explicit list is EXACT
  (an empty list means only the required chain). `skills/loader.py::REQUIRED_SKILLS` is the
  multimedia chain (`image_to_text` / `speech_to_text` / `video_text_to_text` / `text_to_image`):
  the catalogue marks it `required`, serves it FIRST (the 技能 tab renders those rows as locked
  chips with no checkbox), the service 400s on a payload that drops one, and
  `build_system_prompt` unions them back for a stale register value. `workspace/prompt_builder.build_system_prompt` reads the
  session's selection through `runtime/session/agent_config_view.py::session_skill_names` and
  passes `exact=` to `skills.loader.get_skills_text`; an explicit argument still outranks it.
  Because the index lives INSIDE the cached system prompt (`system_prompt_injection`'s three-tier
  cache), `agent_config_service.invalidate_session_prompt` clears that cache whenever a payload
  lands — at the live write and at the turn-boundary promotion for a parked one
  (`session_settings_service.promote_pending_settings_sync`) — and it deletes only
  `StateKey.SYSTEM_PROMPT`, never the frozen persona snapshot.
- **Middleware options** (`AGENT_CONFIG["middleware_options"]`): a REQUIRED entry cannot be
  switched off, so its tuning rides here — today exactly one pair, `Summarization.nudge` (the
  中间件 tab renders it as its own section with a switch). The service rejects an unknown section /
  option / non-boolean value and refuses `nudge: true` beside a payload whose tool list omits
  `memory` (the nudge writes memory THROUGH that tool). At runtime
  `agent.middlewares.agent_switch.middleware_option(session_id, "Summarization", "nudge")` gates
  both nudge entries — the compression-time memory review
  (`schedule_memory_review`, handed the messages about to be discarded and run
  CACHE-WARM: the slice goes back VERBATIM on the model configuration the turn
  used — `nudges._build_session_model` rebuilds it from the descriptor
  `thinking_control.current_session_model_config` publishes every main-chain
  call — with the session's system prompt and tool face, so the compaction's
  prefix cut makes the request a prefix-cache hit on the main model and only
  the review instruction is fresh input; `NUDGE["memory_review_mode"] =
"digest"` falls back to the folded view, `summarization/digest.py`) and the
  plan-completion extraction fork
  (`maybe_trigger_plan_extraction`, which replaced plan extraction's old home
  on the compaction path: it fires from `PlanContinuationEnforcer.aafter_agent`
  on the AUXILIARY model with no conversation history, and dedupes on a plan
  snapshot fingerprint under `StateKey.NUDGE_PLAN_SNAPSHOT_HASH`);
  the panel's switch is DISABLED while the draft has memory off —
  a display/run-time condition, so nothing extra is stored and re-enabling memory brings the
  nudge back. Of the built-ins only 全量 leaves it alone (absent = on): 纯净 / 编程助手 /
  情感陪伴 pin `nudge: false`.
- **Middlewares**: FOUR optional entries are switchable (`PlanContinuationEnforcer`
  — renamed from `TodoContinuationEnforcer`, which old preset payloads still name via
  `agent_switch._RENAMED_MIDDLEWARES` — `TaskIntentMiddleware`,
  `DynamicReminderMiddleware`, `SubagentCompletionDrainMiddleware`); each hook early-returns through
  `agent/middlewares/agent_switch.py::middleware_enabled`, and that helper answers `True` for
  every REQUIRED name (a payload written before an entry was promoted cannot switch it off).
  `WorkspaceNoticeMiddleware` and `MultimodalProcessor` joined the required set: the chain
  build now fails without them (`scaffolding._MAIN_REQUIRED`, 16 entries) and the service
  rejects a payload naming one. Together with `system_prompt_injection`,
  `ThinkingControlMiddleware` and `ToolSelectionMiddleware` they are LOCKED in the UI — chain
  membership and order never change.
- **Subagent models**: `subagent_models[role]` is a profile descriptor (the same shape as the
  session main-model override); at spawn `resolve_role_model_profile` reads it for the
  requester session and the child LLM is built with `build_main_llm_for_profile` (provider /
  key / base_url can change), with the role's `model_tier` as the fallback. `steer` re-reads the
  same profile so a steered run keeps its model.
- **代理模型** is the 子代理模型 tab renamed, split into 主代理 (the main agent's model — a profile
  picker whose "follow the environment config" entry maps to `null`) and 子代理 (the per-role
  pickers). It is a PRESET-level field (`PersonaPreset.main_model`, applied through
  `PUT /sessions/model` so the session-model control stays the single owner of that key) rather
  than part of the agent payload. The 当前会话预设 viewer puts 代理模型 FIRST (the session's own
  model pair is what it is opened for), and BOTH of its sub-tabs write through the
  session-model store — 主代理 is literally the same control as the toolbar's model switch
  (one store, one endpoint, so the two can never disagree; the toolbar shows the parked
  change with its 下一轮生效 badge) and 子代理 keeps the role pickers.
- Presets carry the block (`PersonaPreset.agent`, no Dexie version bump); 保存预设 stores it,
  应用 writes it to the open session (the new-session dialog writes it for the session it just
  created), and the read-only 当前会话预设 tab summarises it (skills read-only there, the
  subagent-model picker editable).
- **The agent-config tabs split into sub-tabs** where the catalogue has two natures: 工具 =
  内置 / MCP (`group === 'mcp'`), 技能 = 内置 / 第三方 (`builtin` flag), 代理模型 = 主代理 /
  子代理. The pills are a VIEW filter — the preset's draft stays whole, and the count (已选 n/m) spans the whole catalogue.
- **Four built-in presets**, in display order — the whole spectrum
  (`client/app/composables/persona-catalog.ts::BUILTIN_PRESETS`): 纯净 (nobody named, every
  persona file empty, only the catalogue's REQUIRED tools and skills, every optional middleware
  off), 编程助手 (the operating-rules template, empty soul / user profile, no roles, every tool
  EXCEPT the memory store, the required skills plus `CODING_SKILLS` — code-wiki / taskflow /
  ulw-execute / todolist — and every optional middleware on), 情感陪伴 (the full role-play
  persona with the 任务与计划 / 子代理 tool groups off, every optional middleware off, the
  mirror-image skill set — the required chain plus every skill 编程助手 does NOT pick — the
  task-orchestration sections stripped out of 运行守则 — `stripTaskSections`, a per-language
  heading/label filter pinned by a test that reads the four shipped templates — and the
  couple-relationship line stripped out of SOUL — `stripRelationshipLines` plus
  `COMPANION_DROPPED_RELATIONSHIP_MARKERS`, the same per-language-template pin, so a companion
  session keeps the persona's warmth without the shipped templates' romantic pairing) and 全量 (the
  DEFAULT — the same full persona with everything on and every skill in the index).
  Their agent blocks are DERIVED from `GET /agent/catalog` at apply time
  (`builtinAgentConfig`), never hardcoded — a preset whose restriction cannot be computed
  throws instead of silently applying every tool. The `sherry` id names 全量 (the label
  changed; sessions already bound to it keep the stored name), and its shipped character is
  still 橘雪莉.

## User Login (`server/service/auth_service.py`)

Opt-in login protection in front of the API, shipped OFF: a default install has
no account and `auth_settings.enabled = 0`, so nothing changes until someone
configures it.

**Two independent auth layers.** `gateway_auth_middleware` (Origin allowlist +
per-boot token) is unchanged and still the outermost gate;
`server/trigger/auth_user.py::user_auth_middleware` is the login gate and runs
last, so an unlisted Origin or a hostile page is refused before a session is
even looked at. The per-boot token is re-fetched by the CLIENT before every
socket (re)connect (`requestApi.refreshGatewayToken`, called from
`ws-ticket.ts::prepareWsTicket`), so a page that survives a backend restart
heals within one reconnect cycle instead of replaying a dead token until a
manual reload. `server/trigger/cors.py::cors_origin_echo` (an `after_request`
handler) reflects the caller's Origin for allowlisted origins — credentialed
cross-origin fetches are refused by the browser when the response says `*`.

**ONE account, password only.** There is no username anywhere: the single
`auth_account` row (`CHECK (id = 1)` — the schema cannot hold a second) is
identified by nothing and login takes just the password. The JWT carries only
`sub`/`jti`/`typ` — no personal data.

**Inbound rate limits (design concept #28).** Two fail-open token buckets in
`server/trigger/rate_limit.py` (`WS_RATE_LIMIT`): one inbound `/sessions/ws`
frame per connection (burst 50, 10 frames/s; a starved burst is answered with
ONE `rate_limited` frame — the transition edge, never per dropped frame), and
`/auth/ws-ticket` mints per client address (30/minute). `enabled: false`
restores unlimited behavior; a limiter crash allows everything.

**Brute force is throttled, injection has no surface.** `_LoginThrottle`
(`server/service/auth_service.py`) counts consecutive failures per CLIENT
SOCKET ADDRESS and escalates: `AUTH["login_max_attempts"]` free tries, then
`login_lockout_base_seconds` doubling to `login_lockout_max_seconds`
(a locked client is refused BEFORE any hashing, 429 + `retry_after`), and a
success clears the streak. Every query in the store is parameterized
(`?` placeholders only — never string-built SQL), which the test suite pins
with SQL-shaped passwords, and a missing account still pays one dummy scrypt
verify so timing says nothing.

**Who must log in.** Loopback clients never do (`request.ip_addr`, the socket
address, not a header) unless `SHERRY_AUTH_REQUIRE_NON_LOOPBACK=0`; enforcement
requires BOTH the runtime switch in SQLite AND an existing account, so an enabled
switch can never lock a deployment out of itself. `/auth/status` reports the same
rule the middleware applies.

**Session.** scrypt-hashed password (`server/utils/password.py`, stdlib only,
parameters carried in the stored string), HS256 JWT (`server/utils/jwt_utils.py`,
`algorithms` pinned, `typ` claim separating access/refresh) in two HttpOnly
cookies: `sherry_session` (Path=/, 12h) and `sherry_refresh` (Path=/auth/refresh,
7d). The client never sees a token string; `/auth/login` and `/auth/refresh`
answer `{expires_in, user}` and the browser keeps the cookies. Refresh ROTATES
(the presented `jti` is blacklisted — a replay gets 401); logout blacklists both
`jti`s; `auth_service.start_blacklist_cleanup()` prunes expired entries.

**WebSockets** carry no peer address in Robyn, so their gate is a ticket:
`GET /auth/ws-ticket` makes the loopback decision over HTTP and the handshake
spends the single-use ticket (`?ticket=`). The client prefetches one per connect
(`client/app/composables/ws-ticket.ts`, a lazy import so the socket layer does
not pull the whole auth bridge into every page).

**An expired session cannot strand the app.** A 401 that survives the transport's
rotate-and-replay (`requestApi.rotateSession()` → `'refused'`, i.e. the backend
ANSWERED and rejected; an unreachable backend never signs anyone out) emits
`auth:session-lost`; the auth-guard plugin clears the stale user and routes to
`/login`, so the form appears without waiting for the next navigation. The
scheduled rotation does the same on a refusal. Two related rules keep the guard
honest: a `/auth/status` probe that fails leaves the verdict UNKNOWN (never
"no login required") and stays retryable — the guard must not bounce a user away
from `/login` on a failed probe — and the connectivity banner reports a dead
socket as "backend unreachable" (the `navigator.onLine` hint is unreliable in
containers/webviews; it only fills in when there is no WS verdict yet) and stays
silent while nobody is signed in (the socket cannot connect by design then).

**Storage** is `src/data/auth.db` (`server/DAO/auth_store.py`): `auth_account`
(the single row: scrypt hash), `auth_settings` (single row = the runtime
switch; `SHERRY_AUTH_ENABLED` only _seeds_ a fresh row) and
`auth_jwt_blacklist` (`expires_at` in JWT seconds). Server tests isolate it with
the autouse `isolated_auth_store` fixture — no test may touch the real file.

**Client**: `pages/login.vue`, `middleware/auth.global.ts` (per-navigation
`/auth/status` decision), `stores/auth.ts`, `pages/home/components/AccountSettingsPanel.vue`
(menu → 账户, a right-sidebar tab), `composables/bridge/auth.ts` (raw transport:
codes in, no generic toasts) and the transport's own 401 → refresh-once → replay
(`requestApi.refreshSessionOnce`, single-flight).

## Background Commands: Command-Level Cancel (`agent/tools/terminal_bg_registry.py`)

`terminal(run_in_background=True)` starts a child that OUTLIVES the turn, so stopping it cannot go through the turn's cancel: the WS `stop` frame cancels the turn's own asyncio task, and the background child is a separate process group with its own watcher. This feature is the missing verb — stop ONE command, leave the turn and the session's other commands alone.

- `BackgroundTaskRegistry.kill_task(task_id, session_id=None)` reuses `kill_session_tasks`' tree kill (the process group recorded at spawn, SIGTERM → bounded SIGKILL) scoped to one id. It refuses (returns False, signals nothing) for an unknown id, another session's task (the id IS the ownership boundary), and one whose process already exited. `kill_session_tasks` (session deletion) is unchanged.
- The watcher keeps the story straight: after `proc.wait()` a task whose status is already `killed` only records `exit_code` (a killed process exits non-zero, and rewriting that as `failed` would make the panel flicker killed → failed); the exit frame carries `status` + `reason` (`killed` / `exited`), and the auto-turn notice says "stopped by user" instead of "FAILED". A natural failure still reads `failed` / `exited`.
- Two entry points, one method: the agent's `terminal_bg(action="kill", task_id=...)` (main-agent-only, like the other actions) and the UI's `terminal_bg_kill` WS frame, which answers `terminal_bg_kill_ack {ok, task_id, reason}` and lets the watcher's `terminal_bg_exit` confirm the final state.
- Live updates: `_run_background` pushes `terminal_bg_started` after registering the task, so an open popover adds the row without polling; `GET /terminal/bg?session_id=` is the snapshot for mount / reconnect (the registry is process-level and NOT persisted — after a restart an empty list is correct, not lost work).
- The display lives in the toolbar's terminal entry — the third icon above the input box (`TasksButton.vue`), whose popover already lists running sub-agents and foreground tool calls; its third section is `BackgroundCommandsSection.vue` (command / status chip, pid in the row tooltip, 终止 on each row), rendered only while the session has commands STILL RUNNING — a settled command leaves the FRONTEND list (its notification is the chat's background card, its log stays on the backend and its record stays readable through `terminal_bg`), and the header counts the running ones only. Nothing about background commands is persisted client-side: the composable is in-memory (snapshot + frames), and no Dexie table holds them. The entry's badge counts sub-agents + foreground calls + running background commands, so a lone detached command is still visible. `use-background-tasks.ts` owns the state (snapshot + the three frames) and `index/[sid].vue` passes it down, so the section is a pure view. It used to be a collapsible strip under the message list; that slot is now free for the chat.
- Freshness is the SNAPSHOT's job, not one frame's: `terminal_bg_exit` is a best-effort session broadcast and is silently dropped when no socket is registered for the session (verified live 2026-10-30: a command killed between turns stayed "running"), so the popover re-pulls `GET /terminal/bg` every time it OPENS (a cache-busted `_=<ms>` param — a snapshot is state, never a cacheable document) and the page re-pulls once ~1.2 s after a kill click. A requested stop renders its own chip (`backgroundTasks.statusStopping`, amber) instead of a stale "running", and `applySnapshot` drops ids that are no longer running from the in-flight stop set, so the row can never claim a stop is still pending after the snapshot settled it.

## Command Palette (`client/app/components/CommandPalette.vue`)

`Ctrl+K` / `Cmd+K` opens one searchable command list over the things the shell
already knows how to do. It is a thin layer over the existing command
registries, not a second dispatch table:

- `composables/use-command-registry.ts` — `CommandEntry {id, label, section,
  icon?, keywords?, handler}`, `registerStatic`/`registerStaticBatch` (a
  repeated id REPLACES, so a remount or a locale switch cannot duplicate rows),
  `setDynamicCommands`, scoring (exact 1000 > prefix > substring > keyword) and
  `highlightLabel` (escaped first, THEN the match is wrapped in `<mark>` — the
  label is user/page-provided text rendered through `v-html`).
- `pages/home/commands.ts` — the static half is DERIVED from `headerTools` /
  `toolboxTools` (the 菜单 grid is the source of truth, so a new tile appears in
  both), plus the notification bell and 新建会话; handlers call the existing
  `buildHomeToolbarCommands` registry and `useNewSessionStore().openDialog()`.
- The dynamic half (skill index + recent sessions) is pulled on every open and
  replaces the previous dynamic set.
- Search history is Dexie v4 (`commandHistory`, additive table; one row per
  command id, capped 50, newest first), rendered as 最近使用 on an empty query.
- The shell (`pages/home/index.vue`) mounts it, registers the static batch under
  a `watch(locale, …, {immediate: true})` (labels are resolved i18n text — a
  `watchEffect` would retrigger itself because registering reads and writes the
  same registry refs) and owns the document-level keydown (Shift/Alt variants
  pass through; the listener is removed on unmount).
- i18n: `commandPalette.*` in all four locales; command labels reuse
  `toolbar.*`. Sections render session → navigate → tools → skills; the keyboard
  cursor walks the grouped list as one flat index, and a new query resets it.

## Shell Layout: Three Columns (`client/app/pages/home/index.vue`)

The shell is one flex row — left session column, conversation, right sidebar — and each column owns a width band modelled on ZCode's workspace shell. The point of the bands is that no column can be squeezed out of existence by another, and only the columns that are worth less than the chat give way.

| Column | Band | Owner |
| :--- | :--- | :--- |
| Left (`SessionSidebar.vue`) | 280px floor, half the viewport at most, minus what the other columns need; drag the right edge, width persisted | `stores/sidebar-width.ts` (`SIDEBAR_MIN_WIDTH` / `SIDEBAR_MAX_WIDTH_RATIO`) |
| Middle (`NuxtPage`) | `min-width: max(320px, 35%)` — an absolute floor (a message row needs it) plus a proportional one (the chat keeps its share on a large display) | `common/layout.ts` |
| Right (`RightSidebar.vue`) | 280px floor (`RIGHT_SIDEBAR_MIN_WIDTH`), 65% viewport ceiling AND the space left over by the left column plus the conversation's floor (no fixed pixel maximum — the ceiling scales with the window), `shrink` so it gives way when every minimum cannot fit | `stores/right-sidebar.ts` |

- **Both draggable columns clamp against what the layout still needs.** The right column's `clampSidebarWidth(width, viewport, reserved)` takes the left column's width plus `middleMinWidth(viewport)` as `reserved`, so its ceiling is `min(65% viewport, viewport - reserved)` — both ends of every column's band are proportional (left 50% ceiling, conversation 35% floor, right 65% ceiling); without the reserved term a drag on a 1280px window could store 760 and overflow the shell by 80px (280 left + 320 chat + 760 panel), and without a ratio the panel could never grow past 760px on a 4K display. The visible consequence is deliberate: on a 1280px window with the left column at its 280px floor the panel maxes at 552 and the chat sits exactly on its 448px (35%) floor.
- **The clamps compose.** The left column's ceiling is `min(50% viewport, viewport - reserved)`, where `reserved` is the conversation's 320px plus the right column's 280px while it is open (`shellReservedWidth` in `SessionSidebar.vue`). Without that the two fixed minimums would overflow the window on a wide drag. A viewport too narrow for the floor lets the ceiling win: a column wider than the window helps nobody.
- **Narrow-window safety net** (`composables/use-auto-collapse.ts`, thresholds 480 / 360 with a 300ms resize debounce): below a 480px conversation the right column retracts, below 360 the left one does too. It runs once at mount (a layout restored from localStorage can already be too narrow) and then only on WINDOW RESIZE — opening a panel by hand is never undone by the layout.
- **Writability**: the composable writes the two `collapsed` refs, so they must come from `storeToRefs`; a `computed(() => store.collapsed)` throws on write (Vue only warns) and the collapse silently never happens.
- **Not Splitter-based (deliberate).** The conversation and the right column are two flex items, not a PrimeVue `Splitter` pair: when the conversation reaches its 320px floor the RIGHT column starts shrinking, instead of both shrinking proportionally from the start. The layout guarantees minimums, not a ratio. Switching to a fractional split was considered and dropped — the browser/terminal/settings panels host their own resizers, and a second split model on top of them made every nested drag ambiguous.
- **Collapsed is not a width**: the left column's collapsed state stays `w-0` (the parent owns the flag) and expanding returns to the persisted width. The right column keeps its `maximized` mode (`fixed inset-0`), which the `shrink min-w-[280px]` classes must not touch — the class list keeps them in the expanded branch only.
- **The right sidebar's tab strip is an equal-width, proportional-shrink row**
  (`RightSidebar.vue` + `composables/use-tabs-overflow.ts`): every tab is
  `flex-[1_1_9.75rem] min-w-15 max-w-39` — a 156px basis and ceiling, a 60px
  floor — so a tab's width never follows its title (白板 and 浏览器 2 are the
  same width) and a crowded strip narrows ALL tabs equally before anything
  scrolls. Only when N tabs at their 60px floor no longer fit
  (`N*60 + (N-1)*4 > viewport`) does `overflow-x-auto` take over, with the
  scrollbar hidden and a gradient fade mask (`tabs-mask-left/right`, anchored
  to the wrapper so they do not scroll with the content) marking the cut-off
  side. The title fades at its right edge with a `mask-image` instead of an
  ellipsis (the span is the tab's flexible item, so the close button's width is
  already excluded — no per-state offset), and the close X is always in the
  layout but only fully opaque on the ACTIVE tab (idle tabs show it on hover),
  so hovering never shifts the title.

The toolbar keeps ONE app-wide switch — the theme toggle, immediately left of the 菜单 button. The interface language lives in the 菜单's FIRST entry, 系统配置, as its LAST tab (国际化): the panel is the app-wide settings surface, and a user who cannot read the current UI finds the language before anything else there. The tab is appended last on purpose — the panel's footer / embedder-refresh / dirty-state logic is keyed by tab INDEX, so inserting it anywhere else would silently re-target those rules. Every switch funnels through `useLocaleSwitch` (`setLocale` **plus** the `i18n_redirected` cookie `app.vue` reads at boot — `setLocale` alone switches the session and forgets the choice on reload).

## Session Token Budget (`agent/middlewares/token_budget/`)

A session can carry an optional token CAP (`PUT /sessions/budget`,
`StateKey.TOKEN_BUDGET_CAP`; `null` clears it, `0` means unlimited). The cap is
optional and absent by default, so the middleware is a pure pass-through until
someone sets one. `TokenBudget` sits beside `IterationBudget` in the main chain
(always on, not gateable): `wrap_model_call` / `awrap_model_call` first check the
cap and, once `TOKEN_BUDGET_USED >= cap`, return a terminal `AIMessage` WITHOUT
calling the provider (the turn ends with a summary request). After a normal
response it accumulates the provider-reported `usage_metadata` into
`TOKEN_BUDGET_USED` (exact) and `TOKEN_BUDGET_COST` (estimated USD), dual-written
to the mem + DB registers so a restart does not lose the counters. The cap/park
twin (`TOKEN_BUDGET_CAP_PENDING`) uses the same busy → park / turn-end promote
rule as the thinking/model controls; `POST /sessions/budget/reset` clears
USED/COST/LIMITED but keeps the cap. The UI is a SESSION-scoped right-sidebar tab
(`tokenBudget`, `client/app/pages/home/components/TokenBudgetPanel.vue`): the cap
belongs to one conversation, so it is NOT a 系统配置 tab any more. It is opened
from the context-ring popover's footer entry (`ContextUsageButton.vue` →
`openTab('tokenBudget', { sessionId })`, which also closes the popover) and takes
its session from the tab payload like every other session panel; it shows a
no-session state only if the payload lacks one.

**Inheritance** (`TOKEN_BUDGET_DEFAULT_CAP` in `sherry.jsonc`, 0 = unlimited):
the session's OWN value always wins — `0` there means explicitly unlimited, so a
positive app default never resurrects a session the operator cleared — and only
a session that never set anything inherits the default
(`agent/middlewares/token_budget/core.py::effective_cap`, cached per process
because the value sits on the per-model-call path and sherry.jsonc applies
values after a restart). `apply_token_budget_choice(None)` clears the session's
override (back to inheriting); `0` is the explicit-unlimited choice the panel's
toggle / an emptied cap field writes. `GET /sessions/budget` reports
`{cap (enforced), session_cap (own choice), default_cap, inherited, used, cost,
limited, pending}` so the panel can say where the cap comes from.

**App Config tab** (`config/sherry_settings.py` registry → `GET /sherry-config`):
each entry now returns its typed `default` alongside `value`, a registered key
the file does not carry yet is still LISTED (with its default; the next write
appends its line before the closing brace and fixes the previous comma), and the
panel renders a localized label + one-line hint per key from
`SHERRY_ENTRY_SLUGS` (an unknown key falls back to its raw key with no hint).
「重置为默认值」 fills the form with those defaults (the footer's 保存 still
writes them), and it is disabled once nothing differs.

Pricing is USER-CONFIGURABLE, not hardcoded: `.models` carries
`MODEL_PRICING_TABLE` (a JSON object serialized as a JSON string so the flat
key/value file editor keeps working; seeded from `.models.example`, and an
existing `.models` gains it on import). Lookup order is provider-returned cost
(OpenRouter-style `usage.cost` / `prompt_cost` + `completion_cost`) → the user
table → the local litellm cache (`src/data/litellm_prices.json`, gitignored,
refreshed by `scripts/sync_litellm_prices.py`) → the table's `_default`
(`config/features/infra_side/model_pricing.py::resolve_model_pricing`). The cap
uses tokens only; the cost is an estimate for display/warnings. A warning event
(`budget_warning`) fires once at `MODEL_PRICING["budget_warn_threshold"]` (0.80)
and a `budget_limited` event fires once when the cap is first reached; both are
best-effort WS pushes. `GET /sessions/budget` reports
`{cap, used, cost, limited, pending}`.

## TaskFlow Progress (`agent/tools/taskflow/progress_push.py`)

Two reporting views, both pushed (never polled) to the chat's floating progress
panel — top-right overlay in `ChatBox.vue`, collapsed to a pill by default:

- **Todos** — the existing `todo_updated` push from
  `agent/tools/todolist/service.py::_push_todo_update`.
- **Steps** — `progress_push.py` sends each flow's flat step list (`step_id`,
  task text, status) plus per-status totals. The scheduler unlocks each step on
  its own `depends_on`; there is no wave/round projection, so the payload never
  implies a barrier between groups of steps. Every flow mutation calls
  `progress_push.push_taskflow_progress(session_id)` — wired into the shared
  `update_flow_with_conflict_retry` plus create / update_steps / finish / fail /
  cancel / set_waiting — which sends `{"event":"taskflow_updated", …}` to the
  session's websocket, best-effort and never raising (a dead socket must not break
  a tool). A reconnecting client asks for a snapshot with `taskflow_refresh`
  (handled in `server/trigger/core.py`) and gets the identical payload, so the
  client has one shape to render (`client/app/stores/taskflow.ts`,
  `pages/home/components/ProgressFloat.vue`).

## Web Search Backends (`agent/tools/web_search.py`)

`web_search` is ONE tool over two interchangeable providers — the Tavily branch
pins `name = "web_search"` (TavilySearch's own default would ship the swap as a
second tool name), so the model, the per-session tool selection and the 工具 tab
all see the same tool whatever serves a call. Which one serves a call is resolved per call
(`resolve_preferred_backend`): the session's explicit choice
(`StateKey.SEARCH_BACKEND`, written by the 模型联网 panel through
`PUT /sessions/search_backend` and parked/promoted at the turn boundary like the
thinking and model controls) → else `WEB_SEARCH_CONFIG["default_backend"]` →
else availability (a default whose provider is unconfigured switches to the
other one; an explicit choice counts only while its provider IS configured, so a
session pinned to a backend that lost its `.network` entry cannot end up aiming at
nothing). Both unconfigured leaves the tool a stub whose result names the fix.
Tavily keeps its bounded retry and then falls back to SearXNG when an instance
is configured — the one thing SearXNG never does is fall back to Tavily (a
self-hosted deployment may have no key at all, and the pair must not ping-pong).

The fallback is announced, not silent: `agent/tools/web_search_push.py` pushes
the best-effort `search_backend_fallback` frame
(`{from, backend, reason, message}`, reason ∈ `tavily_auth_failed` /
`tavily_timeout` / `tavily_error`; an auth failure skips the retries) to the
session's websocket, exactly like the TaskFlow progress push. `client/app/stores/web-search.ts`
turns it into a 通知 entry (source `web_search`, reasons localized from the
CODE at push time through `resolveRuntimeT`, whose `t` takes named params) and
the bell's unread badge lights up — the listener lives in the app shell
(`pages/home/index.vue`), because the frame arrives while a TURN runs and the
user may be looking at another session. The turn it belongs to keeps streaming,
and the result text names its own backend ("SearXNG search results …"), so a
client that misses the frame still shows the truth.

SearXNG lives in `agent/tools/web_search_searxng.py`: the instance's JSON API
(`GET {SEARXNG_URL}/search?q=…&format=json&pageno=1`), results ranked by score
and formatted like Tavily's, plus the three connection options a self-hosted
deployment needs — an httpx proxy, engine/category passthrough, and a
TLS-verification toggle for MITM proxies. It NEVER raises: every failure returns a readable line
naming the status and whether the request went `via proxy …` or over a `direct
connection`. `SEARXNG_URL` / `SEARXNG_PROXY` / `SEARXNG_ENGINES` /
`SEARXNG_CATEGORIES` / `SEARXNG_VERIFY_TLS` live in the gitignored `.network`, and
so does `TAVILY_API_KEY`: the 模型联网 tab is their ONLY editing surface
(`SERVER_HTTP["env_panel_hidden_prefixes"]` withholds `TAVILY_`/`SEARXNG_` from
`GET /env` AND `GET /network-config`, so the 模型配置 tab — `config.tabs.env`,
renamed from 环境配置 — never shows a second, drift-prone editor; writes go
through `PUT /network-config`, reads through the state payload's `env` block,
and the Tavily VALUE never leaves the server). They are read at BOOT, so the panel's save says so — while the
per-session radio applies at once.
`GET /sessions/search_backend` answers the panel's read model (choice, effective
backend, `pending`, both providers' availability and SearXNG's status) and
tolerates an EMPTY `session_id`, because the panel is a settings tab that opens
without a session too. `scripts/dev/mock_searxng.py` implements exactly the call
the provider makes, for exercising the fallback end-to-end without a real
instance (and without a Tavily key: point `SEARXNG_URL` at it and set
`TAVILY_API_KEY` to a dummy to watch the downgrade fire).

## GraphRAG Query Paths (`skills/builtin/core/multimodal_rag/scripts/graph_rag/path_search.py`)

The vendored LightRAG retrieves entities/relations by vector similarity and leaves
multi-hop joins to the LLM, so a bridge entity outside top-k makes a relationship
question unanswerable by traversal. `path_search.py` adds the explicit layer
(the vendored tree only gains a dispatch hook in `operate.py`):

- **Endpoint resolution** — exact graph node name → graph-label containment
  (`Kepler-7 launch pad` → `Kepler-7 Launch Pad`) → entity-vector recall keyed by
  `compute_mdhash_id(name, prefix="ent-")` (the vdb keys are hashes, not names —
  a raw-name `get_vectors_by_ids` silently returns nothing).
- **BFS** — all simple paths up to `GRAPH_RAG_PATH["max_depth"] = 3`, ordered by
  `(hops, -average edge weight)`, capped at `top_k_paths = 10`, with a
  `max_visit_nodes` budget and a `max_frontier` per-layer cap; a path never
  repeats a node.
- **Beam fallback** — only when BFS finds nothing: bidirectional,
  embedding-guided, `beam_width = 5`, `beam_max_hops = 5`, score =
  cosine(neighbour, opposite endpoint) + `beam_edge_weight × edge weight`.
- **Modes** — `mode="path"` searches paths exclusively and falls back to
  `hybrid` recall when none exists (never an empty context); `mix`/`hybrid`
  queries that read as relationship-shaped (four-language patterns,
  `is_relationship_query`) auto-merge a path section. Both add the chain +
  node/edge descriptions + the traversed edges' source chunks to the prompt and
  the structured raw data (`path_info` metadata). Tunables live in
  `config/features/agent_side/graph_rag_path.py::GRAPH_RAG_PATH`.
- **Env defaults belong in `graph_rag/__init__.py`**, before the vendored import:
  `QueryParam` and the LightRAG dataclass read `TOP_K` / `CHUNK_TOP_K` /
  `COSINE_THRESHOLD` / `MIN_RERANK_SCORE` (tuned to 20 / 12 / 0.4 / 0.5) and the
  graph caps into class-level defaults at import time; setting them in `base.py`
  after its own LightRAG import never took effect.
- **Sparse recall is blocked**: `models/embed_model/text/core.py::CustomEmbedding`
  is dense-only (llama.cpp `create_embedding` / a remote `/embeddings`), the vdb
  is one HNSW cosine index, and `EmbeddingFunc` returns one `ndarray`; BGE-M3
  sparse would need FlagEmbedding/PyTorch or a sparse endpoint. FTS5/BM25 remains
  the documented future option for rare ID/version strings.
- **Measurement**: `evals/graph_rag/multihop_bench.py` (20 docs, 102 nodes / 106
  edges, 6 questions, deterministic bridge-token scoring) reports bridge recall
  and context size per mode; `evals/graph_rag/suite.py` stays the single-hop RAGAS
  regression gate. Measured after the change: bridge answer recall 0.875 → 1.000,
  total context −44% (`mix`) / −85% (`path`), path latency 2-13 ms.

## URL Fetching (`agent/tools/web_fetch.py`)

`web_fetch(url)` reads ONE http(s) URL and hands it back as markdown — the
companion to `web_search` (search finds URLs, this reads one). Two branches:

- **public** (default): `is_public_url` (its own DNS resolution) then
  `safe_fetch` — the same SSRF guard + DNS pinning + per-hop redirect re-check
  the media pipeline uses; the body is capped at
  `TOOLS_TIMEOUTS["web_fetch_max_bytes"]` (10 MiB) and the fetch at
  `web_fetch_timeout_seconds` (30 s);
- **local / private** (`localhost`, literal private / loopback / link-local
  IPs): REFUSED unless `SHERRY_WEB_FETCH_ALLOW_LOCAL=1` (tracked `.env`,
  default 0, read per call). Then httpx does the fetch (`verify=False` — dev
  servers and self-signed certs — with a 5-hop redirect cap and a 10 s budget).
  Address classification is literal-only: a public hostname resolving to
  127.0.0.1 still goes down the public branch and is refused there, so the
  switch can never turn a public-looking name into a local fetch. This is the
  deliberate escape hatch for debugging a dev server the agent is running; the
  switch reaches every service this machine can see.

Both branches share the tail: a non-HTML body (JSON, plain text) is returned
VERBATIM (html2text would escape and reflow it), HTML goes through `html2text`
(scripts/styles/noscript stripped first; a tag-stripping fallback if the import
ever fails), and the output is capped at
`TOOLS_TIMEOUTS["web_fetch_max_output_chars"]` (50 K chars) — the context
eviction layer is the second net for whatever still arrives oversized. The tool
is in the `web` catalogue group with `idempotent: False`; it is not
`main_only`, but a specialized child only receives it in `general` (inherit)
mode, since no functional role's allow-list names it. Tests:
`tests/agent/tools/test_web_fetch.py`.

## Log Tracing & the Trajectory Ledger (`server/service/trajectory_store.py`)

A turn is one greppable unit. `turn_runner.WsTurnExecutor._drive` opens
`turn_scope(turn_id, session_id)` around the whole turn — a
`logger.contextualize` scope (every log line in the turn carries both ids,
including the middleware chain, tool calls, LLM retries and compaction) AND a
contextvar the frame projector reads. The WS resume path opens the same scope
with its own fresh turn id, and `_execute_subagent` rebinds the CHILD's identity
(child session key + its own turn id) so a subagent's lines never carry the
parent's. `logs/logger.py` adds a JSONL twin of the full log
(`logs/output/json/json_*.log`, `serialize=True`) so
`jq 'select(.record.extra.turn_id == "…")'` pulls one turn out.

`trajectory_events` (SQLite, `SESSIONS_DIR/trajectory.db`) is the structured
per-turn timeline for the UI: `ServerStreamTurn._project` hooks the stream's
frame yield sites and upserts `tool_start`/`tool_end` into ONE row per tool call
(deterministic `event_id`, `json_patch` merge: args from the start, result +
duration + error from the end), the turn scope records `turn_start`, the stream's
completion records `turn_end` (total ms + TTFB), and agent-side producers
(Summarization's compaction) reach the ledger through
`runtime.hooks.RECORD_TRAJECTORY_EVENT` (registered at server assembly) —
`agent/**` never imports the server module. `GET /sessions/:session_id/trajectory`
(`server/trigger/http/trajectory.py`) reads a turn back (`turn_id`, `limit` ≤ 500,
`offset`); writes are per-session pruned to `TRAJECTORY["max_turns_per_session"]`.
Everything in the ledger is fail-open: a metrics write never breaks a turn.

The message store now carries the same identity: `messages.turn_id` (additive
`TEXT` column + a partial `(session_id, turn_id)` index) is stamped by
`_persist_batch` from the server-owned `current_turn_id` contextvar through
`runtime.hooks.GET_CURRENT_TURN_ID` (`context_engine` never imports `server`).
Every persist batch of one conversation turn therefore shares the uuid while
keeping its own per-batch `turn_num`; the same values also ride on
`BaseMessage.additional_kwargs["turn_id"] / ["turn_num"]`. A missing hook or an
inactive scope stamps `""`; rows written before the column stay `NULL`. This is
the join key between `messages` and `trajectory_events.turn_id`.

## Agent-Controllable Browser (`config/features/infra_side/browser_agent.py`)

Opt-in and OFF by default (`SHERRY_BROWSER_AGENT_ENABLED=1` + restart; the
「模型配置」 panel grows a `SHERRY_BROWSER` group for its switches (they live in
`.network`)). With the feature off no browser tool is registered, every
`/browser/*` route answers 404 and no Chromium is ever spawned.

**Server side** — `server/service/browser_cdp.py` is a thin CDP transport (one
WebSocket, JSON-RPC ids, flat sessions, _droppable_ screencast events) plus the
launcher: it probes `--no-sandbox` last (the first attempt is capped at 6 s so a
host without user namespaces pays seconds, not the whole launch budget) and
honours `SHERRY_BROWSER_HEADLESS` (default: headed when a DISPLAY exists). The
process singleton `browser_manager.py` keeps pages per session, stores snapshot
refs SERVER-side (cleared on navigation), bounds every verb (timeouts, element /
text / screenshot caps), evicts the LRU past `max_pages`, reclaims pages unused
for `idle_timeout_s` unless a panel is watching them, and reaps its Chromium on
exit (`shutdown_browser_blocking` signals the exact PID). The debug port never
leaves the process: `status()` reports the page count, never the port, and the
devtools frontend is built in-process.

**Routes** — `GET /browser/status`, `POST /browser/page|navigate|close` (the
panel's HTTP face) and the live channel `/browser/ws` (gateway token + login
ticket): screencast frames out, `nav`/`reload`/`back`/`forward`/`viewport`/
`input`/`watch`/`devtools` in. A user-initiated navigation reaches the address
bar through `Page.frameNavigated` (a `chrome-error://` page keeps the URL the
user asked for); the screencast stops when nobody watches a page.

**Agent side** — the merged `browser` tool (action =
navigate / snapshot / click / type / press / scroll / screenshot) lives in the
`browser` catalogue group, is MAIN-AGENT-ONLY (`metadata["scope"] =
"main_only"` — the subagent tool policy drops it unconditionally), and is taught
by the `browser` playbook skill (`skills/builtin/core/browser/SKILL.md`,
scriptless, hidden from the index while the feature is off). The seven verbs are
thin handlers over the same manager; the original `browser_*` classes remain
internal handlers and are exposed only by `build_legacy_browser_tools()` for
tests. `browser(action="screenshot")` writes a PNG and
returns **the image itself** when the serving model reads images natively
(`media_pipeline/tool_media.py::native_image_content` — the `image_to_text`
skill is the fallback for a model without vision, never the route a
vision-capable one takes; the per-request capability scrub and the retry
fallback strip the block again for a model that turns out blind, and the
per-turn history strip removes it from later turns so a screenshot does not
ride every context); `browser_evaluate` stays a separate tool and registers only
with `SHERRY_BROWSER_ALLOW_EVALUATE=1`.

**Panel** — `BrowserPanel.vue` picks CDP mode from `/browser/status` and falls
back to its iframe mode otherwise (`composables/browser-channel.ts` owns the
socket). Free size drives REAL device emulation
(`Emulation.setDeviceMetricsOverride`) and pointer coordinates map through the
emulated viewport, never through the screencast frame's pixel size (Chrome's
mobile emulation reports its own scale, measured 393x852 -> 554x1200); 打开调试
工具 opens the inspector as a page the panel watches. There is NO approval gate
on browser actions — the panel IS the human's window on the same page (ZCode
parity); the external-path HITL gate still covers `terminal`/skills.

## Stream Broadcast & Reconnect Resume (`runtime/session/ws_push.py`)

Every streamed frame fans out to **all live sockets of the session** at send
time (`runtime/session/ws_push.py::broadcast_to_session`, called by the two
`StreamDriver.send_frame` sites and the ~14 push sites: turn errors, taskflow
progress, todo pushes, web-search fallback, background-terminal exits, subagent
events, heartbeat/cron pseudo-sessions). Process-level work that is NOT
session-scoped uses `broadcast_to_all` instead (every registered socket,
whatever its session; model download progress is its first consumer). The
registry keeps a per-session bag
(`relation_register.session_id_to_websocket_ids`, 1:N, ordered) plus the 1:1
"newest socket" view; a failed send prunes only that socket. **There is no
replay log and no per-client queue**: each frame reaches each socket exactly
once, and a socket that connects mid-turn receives only frames produced after
its registration.

A viewer that drops mid-turn reconnects and makes ONE HTTP request instead of
a WS snapshot frame: `GET /get_history_by_turn_page?...&include_active_turn=1`
returns `{messages, active_turn}` (without the flag the endpoint keeps returning
the plain row array). `messages` carries every turn that completed while the
socket was down, so no answer waits for the current turn to end; `active_turn`
is `active_turn_snapshot()`'s `{text, has_reasoning, seq, turn_id, turn_num}` or
null. While that request is in flight the socket HOLDS every inbound frame
(`AgentSocket.holdFrames`, capped at 1000, oldest dropped); on the response the
client merges history, applies the snapshot to its turn's row (by `turn_id`,
else `turn_num`, else a new AI row), records the watermark and releases the
queue. A held frame is dropped only when its `turn_id` matches the snapshot's
and its `seq <= snapshot.seq`: the watermark is PER-TURN (each `StreamTurn`
restarts `seq` at 1), so a new turn can never be mistaken for old data. Every
stream frame — chunks and the driver's `turn_started` / `done` /
`hitl_request` / `error` / `stopped` — carries `turn_id`; the client routes by
it, and a late `done` of a previous turn does not settle the current one. The
wait is bounded at 10 s: on timeout the response is gated out of the chat merge,
the queue is released without a watermark, and terminal frames or the busy
watchdog settle the turn. Only a send that already received a chunk can be
concluded as ended-while-disconnected; a pre-chunk send is re-sent (deduped by
`msg_id` while ACTIVE, and also against a recently `DELIVERED` row so a replay
that arrives after the turn finished cannot start a duplicate turn) and stays
pending. The old `resume_stream` WS handler and its
`stream_resume` reply are retained as a legacy fallback, but the client no
longer sends that frame. This snapshot + per-turn `seq` pair is Sherry's
stream-recovery anchor: it restores the text tail without replaying any frame,
while committed tool calls stay queryable in the per-turn trajectory ledger
(`GET /sessions/:session_id/trajectory`; hook events and frame projections share
ONE per-turn sequence — the hooks used to write `seq=0`, so every hook event
sorted before the turn's first frame — timestamps carry milliseconds, and a
failed stream records an `error` event before re-raising, because a timeline
that simply stops reads as "still running"). Because the turn keeps running when
its last viewer disconnects (the socket release no longer cancels it), the
reconnect resumes the SAME turn. Inbound frames are also checked against the
socket's registered session (`_frame_session_allowed`) so one session's socket
can never read another's.

The per-turn trajectory ledger also has a UI consumer: `TrajectoryPanel.vue` is
a SESSION-scoped right-sidebar tab (`right-sidebar.ts` kind/scope, opened from a
tool card's history button or `ChatBox.vue::openTrajectory`). It consumes the
existing `GET /sessions/:session_id/trajectory` only — no backend change —
groups rows by `turn_id`, re-sorts each turn by `(ts, seq, event_id)` (rows
can share a `ts` at millisecond precision, so the server order alone is not
total), and caps at two 500-row pages. It renders tool rows
(name/status/duration/args/result) and lifecycle rows (compact / model_retry /
invalid_tool_call / turn_start / turn_end) with a kind badge; search is
filter-style with an n/m counter, folding is two section switches plus
expand/collapse-all, and a turn menu filters one turn. The pure helpers live in
`client/app/composables/use-trajectory.ts`; `fetchTrajectory` is in
`bridge/session.ts`. No polling and no virtual scrolling in v1.

## Cron Jobs & Skill Binding (`skills/builtin/core/cron/`)

The cron engine is a builtin skill (`scripts/base.py::CronService`, jobs persisted to
`cron_jobs.json`), with the agent-facing facade in `scripts/core.py`, REST wrappers in
`server/trigger/http/cron.py`, and the panel in `client/…/CronPanel.vue`. A job can bind an
ordered list of skill names (`payload.skills`): on every run `_assemble_skill_prompt()` loads
each SKILL.md through `_skill_view(name, caller_scope="background")` and prepends it (wrapped
in an `[IMPORTANT: …]` header) ahead of the job message, counts the use (`bump_use`), and scans
the assembled prompt for injection patterns (warning only). The cron agent keeps its minimal
background tool set — skills arrive by pre-load, not by a `skill_view` tool.

Reference maintenance lives in `scripts/skill_refs.py` and keeps bindings honest when skills
move: `rewrite_skill_refs()` follows consolidation into an umbrella or drops pruned names (called
by the curator right after archiving and by `skill_manage(action="delete")`), and
`referenced_skill_names()` makes the curator's 90-day transition refuse to archive a skill that
some job still loads. Documented in the four-language `scripts/README*.md`.

## Concurrency Lanes (`runtime/lane/`)

Four process-level lanes, each an `asyncio.Semaphore` + active/queued counters, gate concurrent work instead of rejecting it: over-limit work waits FIFO.

| Lane       | Constrains                                                   | Default                                                               | Config key                               |
| ---------- | ------------------------------------------------------------ | --------------------------------------------------------------------- | ---------------------------------------- |
| `MAIN`     | main-agent turn (`_run_executor`)                            | `min(16, max(8, CPU))`, clamped up to `SUBAGENT + NUDGE` (12) → 12–16 | `LANE_SYSTEM["main_max_concurrent"]`     |
| `SUBAGENT` | child-agent executions (spawn + steer)                       | 8                                                                     | `LANE_SYSTEM["subagent_max_concurrent"]` |
| `NUDGE`    | nudge/persistence calls (`summarization/nudges.py`, 3 sites) | 4                                                                     | `LANE_SYSTEM["nudge_max_concurrent"]`    |
| `NESTED`   | `sessions_send` reply turns (serial)                         | 1                                                                     | `LANE_SYSTEM["nested_max_concurrent"]`   |

- Config: `config/features/infra_side/lane_system.py`; `validate_lane_config()` runs at server startup and enforces `main >= subagent + nudge` (all limits ≥ 1); `install_lane_lifecycle()` in `server/service/lane_lifecycle.py` validates, prewarms the manager, registers `set_drain_check(is_gateway_draining)`, and installs a bounded exit drain (`atexit`, `drain_all(timeout=0)` — never blocks exit).
- Queueing: a spawn that passes per-parent admission but exceeds the SUBAGENT lane is registered `PENDING` (no `forbidden`); `PENDING → RUNNING` happens inside the lane slot and only then is `started_at` stamped (queue wait is not run time). `PENDING` counts as active in registry queries and is killable; restore finalizes restart-leftover PENDING runs the same way at startup (silently — no announce), and orphan recovery covers same-lifetime lost tasks with `ended_reason="pending_orphaned"`.
- Drain mode: `set_drain_check(fn)` makes `Lane.acquire()` refuse (without consuming a permit) while the subagent gateway reports draining.
- Observability: `GET /lane-status` → `{main|subagent|nudge|nested: {name, max_concurrent, active, queued}}`.
- Hot updates (`LaneManager.set_concurrency`) only affect new acquires; in-flight slots keep their permits.

## Key Configuration

| File                          | Contents                                                                                                                                                                                                                    |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `config/features/agent_side/` | 57 per-object TypedDicts (summarization, guardrails, tool_result_eviction, iteration, taskflow_infra, todolist_infra, tools_timeouts, step_judge, completion_judge, evidence_ledger, evidence_cross_session, evidence_regression_gate, subagent_isolation, tool_parallel, ...) |
| `config/features/infra_side/` | 30 per-object TypedDicts (gateway, auth, bus, http_upload, retry_backoff, server_http, ws_stream, input_queue, heartbeat, cron, skill_scanner, mes_memory, curator, web_search, model_pricing, model_downloads, ...)                         |
| `config/features/__init__.py` | Aggregator — all 87 TypedDicts + instances re-exported                                                                                                                                                                      |
| `config/path.py`              | All filesystem paths (ROOT_DIR, SKILLS_DIR, WORKSPACE_DIR, ...)                                                                                                                                                             |
| `config/schema.py`            | Pydantic Config (SHERRY_ env prefix, mostly unused at runtime)                                                                                                                                                              |
| `config/sherry_settings.py`   | sherry.jsonc loader (TOOL_CALL_TIMEOUT_MINUTES, LOG_LEVEL, curator._, LANGSMITH._)                                                                                                                                          |

## Code Graph

CodeGraph MCP (`@colbymchenry/codegraph`, wired in `opencode.json`) indexes the repo into `.codegraph/codegraph.db` (gitignored) — a 20-language AST knowledge graph (Python/TS/Rust full; `.vue` gets no AST). One MCP tool, `codegraph_explore`, returns verbatim source + call paths (dynamic dispatch included) + blast radius. Auto-syncs on file events (2s debounce). CLI: `codegraph query|explore|callers|impact|status`. Rebuild index after big restructures: `codegraph init`. It is a snapshot — verify against source when editing.

## Code Intel Graph & Wiki Export

`agent/tools/code_intel/` now carries four edge families, all resolved by the same
two-pass name resolution (`indexer.py`): `call_edges` (unchanged), plus the
parallel `import_edges` / `inherit_edges` / `mixin_edges` tables created with
`CREATE TABLE IF NOT EXISTS` (an old index DB is upgraded in place — C1). The
extractor (`extract.py`) records Python imports/inheritance, TS/TSX named/default/
namespace imports plus `extends`/`implements` (interfaces are symbols), Rust
`use`/trait bounds/`impl Trait for Type`, and Go import paths + struct/interface
embedding. Module-scope edges use `importer_id = 0`; named-edge resolution prefers
the file's own language so a TS `Base` never links to a Python `Base`.

The RESEARCHER surface gained one tool, `path(symbol_a, symbol_b)` — a directed
bidirectional BFS over all four families (forward from A, backward from B,
`code_intel_call_graph_max_depth`) returning hops with `edge_type` and
`confidence` (`EXTRACTED` = resolved, `INFERRED` = name-only). It is an
independent implementation; `impact()` is untouched (C2) and still returns its
`depth` + `via` BFS view. `CalleeInfo`/`CallerInfo` gained a derived
`confidence` field alongside the unchanged `resolved` bool. Query-only methods
(exposed to the agent through `explore`/`path`, not new tools): `imports_of`,
`importers_of`, `inherits_from`, `subclasses_of`, `mixins_of`, `mixins_in_by`,
`god_nodes` (pure SQL degree) and `communities` / `community_of` (networkx
Louvain on demand with a connected-components fallback when networkx is absent).
The tool name is `path`; the tool set is still `scope=researcher_only` and never
joins `_MAIN_TOOLS_BUILDERS` (C5).

`skills/builtin/code_wiki/scripts/core.py::export_wiki_from_index(repo_name,
db_path)` turns a code_intel SQLite DB into a deterministic, zero-LLM wiki
(README/architecture/modules/class-diagram + state); `sequences.md` stays
LLM-owned (runtime flow is not in an AST). It is an independent entry point —
`init_repo_wiki` and the other path helpers are unchanged (C6) — and returns
`{"fallback": True, "reason": ...}` for a missing/empty index or unsupported
languages. Caps live in `SKILLS_TOOLING` (`code_wiki_export_*`).

## LLM Wiki Graph Layer

`skills/builtin/llm_wiki/` keeps markdown as the source of truth and adds a
derived graph at `src/data/wiki/.wiki_graph.db` (`scripts/graph.py`): nodes for
file stems and link targets, edges for `[[wikilinks]]` (`[[Target|INFERRED]]`
carries the tag; a plain link is EXTRACTED), inline links, code-span mentions and
optional frontmatter `extracted_relations`; `extracted_entities` adds nodes.
`build_wiki_graph(force=False)` is incremental by file mtime, removes deleted
files' nodes/edges, and never touches the existing `save_source`/`search_wiki`/
`lint_wiki` signatures (L1). `scripts/graph_query.py` exposes `path` (undirected
bidirectional BFS), `god_nodes`, `explain`, `communities` (networkx Louvain or
connected components) and `query_graph` (ASCII words + CJK bigrams → node recall
→ 1-hop subgraph; no jieba dependency). `scripts/ingest.py::save_binary_source`
adds PDF (pypdf → pdftotext → mineru), image, audio, video, docx/xlsx (stdlib
ZIP/XML readers, no python-docx/openpyxl) and text ingest: one auxiliary-LLM
semantic pass (fail-open), a markdown source with the additive frontmatter, then
a graph refresh. `lint_wiki` appends a `graph` section (connectivity, confidence
split, low-confidence pages, god nodes) after its original ten checks (L7).

## Import Rules

- `server/**` layering: trigger → service → queue|DAO → utils (enforced by import-linter)
- Cross-package bans (enforced by `uv run --no-sync lint-imports`, `[tool.importlinter]` in `pyproject.toml`; grimp counts function-level imports, so a lazy import does NOT satisfy them):
  - `agent/**` MUST NOT import `server/**`
  - `config/**` MUST NOT import `models/**`
  - `context_engine/**` MUST NOT import `agent/**`
  - `workspace/**` MUST NOT import `agent/**` or `context_engine/**`
  - `skills/**` MUST NOT import `server/**`
  - `skills/builtin/**` user scripts may still import `models`/`bus`/`channels`/`workspace`/`runtime` (one-way, by design) — this ban covers `server/**` only; whether to tighten those directions is evaluated separately and is not currently enforced
- Cross-boundary seams: `runtime/hooks.py` (callback registry) and `runtime/data_provider.py` (`PromptDataProvider`/`SkillWriteProvider`) are leaf modules importable from both sides — owners register at assembly time (server boot / `agent.core.init()`), consumers resolve at call time. Never reintroduce a direct import to cross a forbidden boundary. The transcript port is the third seam, in the agent's own direction: `agent/state_port.py` (`read_messages` / `read_interrupt` / `heal_interrupted_turn`) is the ONLY place the LangGraph checkpoint shape is read or rewritten — `server/` drives turns through it and never touches `snapshot.values["messages"]` or `aupdate_state` itself (a turn's INPUT messages and the auxiliary heartbeat agent stay the server's own business).
- `config/features/**` MUST NOT import from `agent/`, `server/`, or `models/` — config is dependency-free
- `config/**` is importable from ALL layers (no restriction)
- `pub/**` may import `config/**` only; `runtime/**` may import `config/**` and `pub/**`. Both MUST NOT import `agent`/`server`/`models`/`workspace`/`context_engine`/`skills`/`bus`/`channels`/`logs`. The read-only shadow HEAD reader lives in `pub/git_head.py`, so `runtime.session.git_head` never reaches into `agent`.
- `logs/**` may import `config/**` and `pub/**` only; every logic package may import `logs`, but `logs` must never import a logic package (the loguru redaction patcher lives in `pub/security/redact_formatter.py`, not under `agent/`)
- every numeric constant lives in a `config/features/**` per-object module (e.g. `SUMMARIZATION` in `config/features/agent_side/summarization.py`), never in a shared `num.py`

## Test Structure

```
tests/
├── agent/
│   ├── middlewares/          # one test file per middleware (summarization, task_intent, ...)
│   ├── tools/
│   │   ├── taskflow/         # test_dag_e2e.py, test_token_budget.py, test_deadline.py, ...
│   │   ├── todolist/         # test_todolist_e2e.py, test_store_sqlite.py, ...
│   │   └── subagent/         # test_spawn_direct_e2e.py, test_queue_e2e.py, ...
│   └── core/                 # test_built_agent_wrappers.py
├── workspace/                # test_prompt_builder.py, test_prompt_builder_todos.py, ...
├── config/                   # test_features_agent_side.py, test_features_infra_side.py, ...
├── server/                   # mirror of server/ source
├── context_engine/           # mirror of context_engine/ source
└── run_tests_split.py        # CI gate: 3-process runner (A=unit, B=integration, C=regression)
```

Markers: `unit`, `integration`, `module`, `system`, `regression`, `llm_e2e` (deselected by default).

`tests/full/` is excluded from the split runner (`--ignore`) and every live-LLM file there is tagged `llm_e2e`, so a bare `pytest` run never collects them. Run one explicitly with `uv run --no-sync pytest -m llm_e2e tests/full/<file>` — addopts default to `not llm_e2e`, so the explicit `-m` is required. Because process startup/teardown dominates the wall clock, run `tests/full/` **one file per process under an external watchdog** (e.g. `timeout 1500 uv run --no-sync pytest -m llm_e2e tests/full/<file>`); measured: 47 cases ≈ 10.8 min of net test time but ≈ 73.7 min wall clock when batched. Hermetic tests belong in the standard tree, not `tests/full/`.

`evals/` sits beside the pytest tree: a homegrown, sandboxed framework with
eleven registered suites (`uv run python evals/evals.py [suite]`; the `graph_rag`
suite
needs the eval-only dependency group — `uv run --group evals python
evals/evals.py graph_rag` — because `ragas` (and the `langchain-community` it
hard-imports) is the harness's only consumer and stays out of the shipped
runtime closure). The external benchmarks are `swebench` (SWE-bench_Lite
instances: pinned base commit, per-instance `uv venv --python 3.11`, the
instance's own FAIL_TO_PASS/PASS_TO_PASS verdict; the test patch is applied by
the JUDGE and a gold-patch miss aborts before any model call; the host harness
quiesces the session before it reads the tree — turn cancel, descendant/background
kill, shadow-git `wait_for_idle` + `freeze` — keeps `.sherry` outside the checkout,
quarantines it atomically on revert, includes untracked files in the saved diff,
and writes a per-instance result immediately), `deepswe`
(DeepSWE tasks: the host checkout is bind-mounted into the task's own Docker
image, only the `terminal` tool is replaced by a `docker exec` bridge, and the
task's separate verifier image grades the agent's `git diff`),
`terminal_bench` (Terminal-Bench tasks: the task image is built, the agent works
inside it through the same bridge, and the task's `run-tests.sh` pytest verdict
judges the run; multi-service compose tasks are reported unsupported),
`agentbench` (AgentBench's official DBBench test set —
https://github.com/THUDM/AgentBench, `data/dbbench/standard.jsonl` from a pinned
checkout, its official table rows materialised in SQLite, the official
`execute_sql` / `commit_final_answer` function names, and AgentBench's own
comparison rules; INSERT/DELETE/UPDATE rows are reported unsupported), and
`toolbench` (ToolBench's official test instructions + official
`test_query_ids` — https://github.com/OpenBMB/ToolBench, with the data mirror
https://huggingface.co/datasets/Adorg/ToolBench pinned for reproducibility — tool
execution through the official RapidAPI proxy, the
official answer format, and the official ToolEval evaluator imported from the
pinned repository). No suite invents its own questions: the container suites
clone their task trees into `~/.cache/sherry_evals` (never the repo); the two
new suites download the benchmarks' own data into the same cache and need
their own keys (`TOOLBENCH_KEY`, `MAIN_LLM_API_KEY`, an OpenAI-compatible
ToolEval judge key); every external suite has a `--dry-run` and SKIPS loudly
with a non-zero exit when its prerequisites are absent.
`coding_capability` is the coding baseline — it drives the REAL main
agent (`server.service.messages.async_generate`) over a self-designed task set,
judges every task with that repository's own `python -m pytest`, fails a task
whose diff touched the tests, pins the thinking switch, and reports provider
failures as invalid runs (excluded from the pass rate) instead of capability.
The suites that repoint `config.path.WORKSPACE_DIR` through
`SHERRY_WORKSPACE_DIR` (coding_capability, swebench, deepswe, terminal_bench,
agentbench, toolbench) re-exec in their own process; `baseline.json` next to
each suite is the version-controlled reference (threshold + model id).

**IMPORTANT**: `tests/agent/tools/subagent/conftest.py` installs sys.modules stubs at collection time. When running multiple test dirs in one process, use the stub-tolerant loading pattern from `tests/agent/tools/taskflow/conftest.py`. The split runner (`tests/run_tests_split.py`) avoids this by running groups in separate processes.

## Conventions

- **Python 3.13** (StrEnum, PEP 695 type aliases, typing.override)
- **Config**: one TypedDict + one instance per feature in `config/features/<side>/<name>.py`, re-exported via `__init__.py` — NEVER use bare dict literals for feature config
- **Middleware hooks**: `abefore_model(self, state, runtime=None)`, `aafter_agent(self, state, runtime=None)` — LangChain 1.3.9 signatures; after_agent runs in REVERSE list order
- **Tool registration**: `@tool("name")` + `build_*_tools()` factory + `_MAIN_TOOLS_BUILDERS` list in `agent/tools/__init__.py`
- **Test fixtures**: `isolated_db` (tmp SQLite), `build_main_tools_real` (stub-tolerant), `scan_skills_real`
- **Docs language** (`scripts/check_doc_language.py`): the parity gate compares
  STRUCTURE across the four languages and the link gate only resolves targets, so a
  label from ANOTHER language used to pass every gate — live-verified twice: Chinese UI
  labels inside English sentences (模型配置, 继续生成 … in the README's Runtime section
  and six wiki pages) and 「模型配置」 inside the Japanese/Korean READMEs. The rules are
  per document language, in ONE table: ENGLISH documents (`README.md` plus every
  `wiki/**/README.md`, nested pages included) may carry no CJK ideographs at all, while
  the JAPANESE / KOREAN documents (`README.ja.md` / `README.ko.md` and their wiki
  siblings) may carry no SIMPLIFIED-CHINESE-ONLY character from the shared list in
  `scripts/data/simplified_chinese_only.txt` — a Simplified-only form (时, 图, 问 …) is a
  Chinese leak, while kanji Japanese also uses is fine. That list is curated, not
  exhaustive: characters whose simplified form is ordinary Japanese (体/国/数/条/后/万/号
  等) are deliberately absent, so a label built only from shared characters (模型配置,
  分叉) is NOT detectable — the gate targets mixed-language prose, not every possible
  label. It ignores what is not prose: fenced blocks, inline code spans, HTML comments
  and markdown link TARGETS (a link may point at a Chinese-named file); the four
  language names (中文 / 简体中文 / 日本語 / 한국어) are allow-listed for the language
  switcher. The Chinese source pages and `AGENTS.md` are never scanned — the first are
  the source language, the second quotes the Chinese labels on purpose. A genuine label
  goes in a code span, never the allowlist. The SAME list backs the client gate:
  `client/app/composables/__tests__/locale-language.ts` (CJK runs / Simplified-only
  detection / leaf walker) is asserted by `locales.test.ts` over the four central
  dictionaries AND by `i18nBlocks.test.ts` over every SFC `<i18n>` block — an `en` value
  may carry no CJK, a `ja`/`ko` value no Simplified-only character (that gate caught the
  same `菜单-预设角色` leak in `en.json` and five inline blocks).
  `tests/scripts/test_check_doc_language.py` pins prose findings, the per-language
  rules, the non-prose exemptions, the allowlist and the file selection.
- **Docs parity** (`scripts/check_docs_parity.py`): gates every four-language README group on structure plus semantic metrics — link-target sets with per-target language variants (a translation that links a foreign README the EN reference does not link fails, while the language switcher and own-language links pass), per-section body-length ratio plus a non-empty check for even the smallest EN sections, a language-independent heading-marker sequence (inline code spans + link targets, order-canonicalized) that fails a heading reordered under an unchanged level sequence, and a per-section invariant-token multiset (code spans + file paths + ENV names + numbers, matched by substring so dropped backticks are not drift) that fails a long but stale translation which lost ≥2 code-span/path tokens or all of them. Section pairing matches headings by marker fingerprint first, then occurrence index (the index-fallback count and the marker-less-reorder residual limitation are printed in the summary). The ratio band is a calibration snapshot: `--calibrate` prints the per-language distribution and its margin, and `tests/scripts/test_check_docs_parity.py` pins the smallest legitimate ratio at ≥1.15× the band so a denser legitimate section turns the test red before the band silently narrows — re-run `--calibrate` and update the constants' comment before changing any band. Exempting a group via `ALLOWLIST` is a last resort: the reason must contain an explicit "Fixing the document(s) is wrong because …" clause followed by a substantive rationale, be ≥40 chars, and may not open with placeholder text; stale entries fail the gate, and tests pin the mapping empty so every addition requires an explicit test edit
- **Commits**: angular conventional (commitlint enforced), pathspec-only (`git commit -- <files>`)
- **File naming**: snake_case enforced by pre-commit hook
- **Line length**: 100 chars (ruff)

## Known Pitfalls

- `workspace/` in `.gitignore` is anchored (`/workspace/`) — live persona files are NOT tracked
- `tests/agent/tools/subagent/conftest.py` pollutes `sys.modules` globally — use stub-tolerant imports in cross-suite tests
- `agent/core.py::init()` imports `skills` lazily and calls `build_skills_snapshot()` at assembly time (server boot); a process that never calls `init()` still gets a live disk scan from `scan_skills(use_cache=True)`
- `Summarization` has both sync (`_apply_compression`) and async (`_aapply_compression`) paths — changes must cover both
- `taskflow_step(action="resume")` and `taskflow_step(action="run_task")` both dispatch via `_dispatch.dispatch_child` — the seam is monkeypatchable
- after_agent hooks run in REVERSE list order — first registered = last executed
- `asyncio.Semaphore` is event-loop-bound, so lanes must be acquired on the main loop — a cross-loop `acquire()` logs a warning and rebinds a fresh semaphore with outstanding slots deducted (never double-issues permits)
- a session's BOUND project directory is snapshotted into
  `<project>/.sherry/shadow-git` before the first write of each turn
  (`agent/tools/pub_base/shadow_git.py::ensure_checkpoint`): write_file,
  patch_file, terminal and python_repl all call it, and it is fail-open — a
  broken store logs a warning and the write proceeds. The user's `.git` is
  NEVER initialized, staged, committed, reset, checked out or worktree'd; the
  shadow store uses `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` isolation and a
  single `refs/heads/main` chain. `info/exclude` carries the deny patterns
  (secrets, bulk dirs and the shadow store) before `git add -A`; snapshots are
  pruned to `SHADOW_GIT["max_snapshots"]` (100 — the store is PER PROJECT ROOT and
  shared by every session including subagents, so a small budget collapsed the
  restore window to the last step or two once a fan-out ran concurrently;
  `ensure_checkpoint` dedupes per session per turn, and the bare store's delta
  compression keeps 100 snapshots at a few hundred KiB). Restore takes a pre-restore snapshot
  and supports one file or the whole tree; the `checkpoint` tool exposes
  list/show/restore. `SHADOW_GIT["store_dir"]` (or `SHERRY_SHADOW_GIT_DIR`)
  moves the store outside the project — the eval harness points it at a
  run-scoped directory so an instance checkout never contains `.sherry` — and
  `freeze()` / `wait_for_idle()` are the write barrier a harness uses to stop
  snapshot writers before it reverts. The git-graph panel reads the shadow chain (branch label
  `checkpoint`), and `git_head` announces `checkpoint@<hash>`. An UNBOUND
  session is untouched, and reads are never gated.
- the three tool classes that used to inherit from the archived
  `langchain-community` / `langchain-experimental` base classes
  (``FormattedWriteFileTool``, ``SafeShellTool``, ``TimedPythonREPLTool``) are
  re-based on plain `BaseTool` with the schemas reproduced locally — the field
  names, descriptions, required sets and the `str -> [str]` commands
  normalization are byte-identical, pinned by
  `tests/agent/tools/test_tool_schema_goldens.py` (which also asserts those
  modules no longer import the archived packages at all)
- file writes go through `file_write_lock` (`agent/tools/pub_base/file_lock.py`: in-process per-path `threading.Lock` → cross-process `flock`) plus `atomic_write_text_no_follow` (`atomic_write.py`); `patch_file` adds a two-layer CAS (fingerprint at read, `expected_revision` re-asserted just before the `os.replace`). Its fuzzy ladder (`pub_base/text_matcher.py`, 11 strategies, first match wins) strips a copied snippet's line numbers (`N|` / `N: ` / `N<tab>`) and unescapes `\uXXXX` before the fuzzy ones, refuses `replace_all=True` for the lossy `BROAD_MATCHERS` (a global replace must not land on a lookalike), re-indents a fuzzy replacement to the file's own indentation and rewrites its straight quotes into the region's curly ones (only when the region uses that kind exclusively — mixed code/prose keeps straight quotes).
- `write_file` needs a READ-BEFORE-WRITE LICENSE to overwrite an existing file: a complete `read_file`, or this session's own earlier whole-file write (`agent/tools/pub_base/read_state.py` — process-local, keyed by session + resolved path, advanced by the session's own append/patch, lost on restart). `read_file` itself is the first context cut: the window is bounded by BOTH `TOOLS_TIMEOUTS["read_file_token_budget"]` (4 000, CJK-aware — the same budget means the same context cost in every language) and the caller's line `limit` (the stricter wins, truncation always at a line boundary), and a re-read of an unchanged file the session READ in full answers with a pointer instead of the content. A single line whose cost alone exceeds `TOOLS_TIMEOUTS["read_file_max_line_tokens"]` (4 000) is clipped inside itself (`...[N chars clipped]...`) and reported `truncated: true` (no write license off it) — WHEREVER the line sits in the page and even when the page budget is switched off, so one minified line can never ride the response whole. Because `offset` counts LINES there is no read_file address for the rest of a clipped line: the hint names the tools that reach it (`search_files` with context, `terminal` / `python_repl`). Its revision rides into the atomic write as `expected_revision`, so a file the session never read is refused and a change landing after the read is refused too; a partial read licenses nothing, `append` alone licenses nothing, a summarized-away read loses its license (`agent/middlewares/summarization/compression.py` drops the license of every read whose `read_file` result the compression discards — the preserved tail's later reads keep theirs, and the scan is fail-open), and a directory/symlink target skips the gate (its write fails with its own error).
- `terminal` / `python_repl` / ast-grep rewrites bypass all of it (subprocesses take no lock) — that boundary is deliberate, not an oversight.
- the skills index (`<available_skills>`, built by `skills/loader.py::get_skills_text`) renders each skill's `description` plus two frontmatter signals — `when_to_use` (**required** on every skill the nudge / curator create or patch, and now carried by every shipped built-in) and `when_not_to_use` (also present on the built-ins) — all three token-bounded CJK-aware (60/50/50 tokens, `truncate_by_tokens`) and XML-escaped; the client's skill tooltip shows the same fields. The curator writes a second, narrower level into the umbrella BODY: every `##` sub-capability section opens with its own `**When to use:**` line (plus `**When NOT to use:**` when a real exclusion exists), and its deterministic fallback extracts both from the merge reasons.
- a taskflow step may bind skills (`skills: ["browser"]`): `build_task_with_dep_results` (the one task-text builder every dispatch path calls) prepends each SKILL.md via `_skill_prepend.prepend_skills`, mirroring cron's `[IMPORTANT: …]` header + fail-open binding + `bump_use` + injection scan — so the child receives the skill directly and the orchestrator never relays it through its own context. `taskflow_flow(action="plan")`'s planner may emit the field and `taskflow_step(action="update_steps")` may change it.
- `terminal` accepts a per-call `timeout` (seconds; default `TOOLS_TIMEOUTS["terminal_timeout_seconds"]` = 30, ceiling `terminal_max_timeout_seconds` = 600 via `_resolve_timeout` — 0/negative means 'unset') and a `run_in_background` flag: a backgrounded command returns immediately with a `bg-<hex>` task id, its output goes to a RETAINED file under the session's `evicted/` (`terminal-bg-<task-id>.log`, never a sweepable temp file) and STAYS there after the command exits — the log is the durable record (`terminal_bg(action="output")` reads it while running AND afterwards; only session deletion removes it), the watcher on the server loop announces the exit (WS frame `terminal_bg_exit` + a `background_task` auto-turn). `terminal_bg(action="list"|"output", task_id=...)` (`scope=main_only`, catalogue group `terminal`) is how the agent reads status and a bounded 8 KiB TAIL; the per-session live task count is capped by `TERMINAL_BG` (10), and `purge_session` SIGTERMs a deleted session's running tasks before its folder goes.
- terminal HITL cards can offer **本会话允许** (`decision: "session"`): it writes the command's two-word pattern to `HITL_SESSION_APPROVED_KEY`, while `approve` stays one-shot. The option is sent only for cacheable ordinary commands; dangerous commands, confirm-all and clawhub never write it, and `check_command` moves dangerous detection BEFORE Layer 6 so a stored pattern can never auto-approve a dangerous command (permanent Layer 5 ALWAYS still wins). The card labels are four-language.
- on Windows the unsandboxed spawn prefers a Git-for-Windows `bash.exe` (`pub_base/shell_resolver.py`, standard paths then inferred from `git.exe`, cached per process): the tool promises a POSIX shell, and `cmd.exe` would quietly change what `&` / `&&` / `$()` / `2>&1` mean. No Git Bash → the platform shell.
- the eviction PREVIEW marks a mid-line cut instead of letting it read as a line ending: the in-memory renderer appends `\u2026`, the file-on-disk reader detects the cut by peeking past `_PREVIEW_LINE_MAX_BYTES` (and, for the tail window, by checking whether the byte before the walk's stop is a newline), and either path appends one note naming the same fallback tools — a normal preview is byte-identical to before (the note is absent, never empty).
- `terminal`'s stdout is fd-direct-written: BOTH spawn sites (`_execute_sync` and `_arun`) hand the child a FILE for `stdout` (stderr merged into it via `STDOUT`, unchanged) opened as `.sherry-tmp-stdout-*.log` in the session directory, then wait on the process instead of draining a pipe. Under `TOOLS_TIMEOUTS["fd_write_threshold_bytes"]` (100 000 bytes) the file is read whole and unlinked; at or above it, it is renamed to `SESSIONS_DIR/<session_id>/evicted/terminal-<sha256(first 64 KiB)[:16]>.log` and only a head/tail preview comes back (`pub/func/message/eviction.py::build_file_preview` → `_read_head_tail`, byte-budgeted: 8 KiB per side and 2 KiB per line, so a newline-free 500 MB output is still a few KB — the total is `stat().st_size` BYTES, lines are never counted). A timed-out/cancelled/failed run discards its temp file; leftovers from a hard kill are swept by the shared `.sherry-tmp-` + `.swp`/`.log` contract in `atomic_write.py` (sweeping runs on the way into the next `_open_stdout_tmp`). A 0-byte result becomes `(no output)` before the tool returns — never an empty ToolMessage, which the pairing sanitizer would otherwise turn into a misleading missing placeholder — and the eviction preview starts with `[evicted to: <path>]` plus an explicit `read_file` hint.
- the sandbox IS the write barrier for `terminal` / `python_repl` / `execute_code`
  (they never call `resolve_external_path`): `SandboxBackend.wrap(cmd, env,
writable_roots)` takes the caller's write surface, which each tool resolves from
  the SESSION's project directory (so a project bound outside the checkout is
  writable and the checkout itself is not), `TEMP_DIR` is appended centrally, and
  an empty list raises. The tracked `.env` ships `SANDBOX_POLICY=auto` (the same
  as the unset fallback: the packaged desktop app has no OS sandbox backend on
  macOS/Windows, where `required` would refuse every command — a server
  deployment that wants the hard guarantee sets `required` itself) — boot logs
  the effective policy +
  backend, an unusable `required` logs an ERROR with fix commands,
  `SANDBOX_STRICT_BOOT=1` refuses to start, and `GET /sandbox/status` reports the
  same state. Every bare run is recorded: the evidence-ledger row carries
  `sandboxed: false` and the bypass approval card is marked `⚠ UNSANDBOXED`.
  `PathGuard` additionally screens the command-string arguments (`commands` /
  `query`) against the hard-deny floor AND the sherry-self policy — read-looking
  tokens against the read allow-list, extracted write targets (`>` / `>>` /
  `tee` / `truncate` / `shred` / `sed -i`) against the write allow-list, so
  `cat .models` and `echo x > agent/core.py` are refused; base64, shell
  variables and runtime-built paths escape that static scan, which is why the
  file tools' gate and the OS sandbox remain the real ones. The read-surface whitelist
  (`SANDBOX_READ_ALLOWLIST`, system trees + `SANDBOX_READ_EXTRA`, else the whole
  fs is readable) is OFF until `scripts/sandbox_smoke.py` passes on a host whose
  sandbox actually runs — neither backend has ever been verified on one.
- **Cross-agent coordination is advisory and opt-in** (`COORDINATION`, env
  `SHERRY_COORDINATION_CLAIMS` / `SHERRY_COORDINATION_FINDINGS`, both OFF):
  `claim_scope` records a per-path intent for this session and `write_file` /
  `patch_file` attach ONE `claim_notice` line when a peer holds a claim on the
  written path — the write always succeeds (no refusal path: claim → license →
  CAS would stack three decisions into one). The notice is deduped per
  (session, path) in a 10-minute window because the agent layer cannot see a
  turn id, and claims expire after `claim_ttl_seconds` (1800). The findings
  board adds `share_finding` (FAIL / FACT / PATCH) and the read-only
  `peers_findings` query (every whitespace term must match text or file paths,
  optional kind, capped); a merge publishes a PATCH entry on its own when the
  board is on. Storage is ONE file, `<project>/.sherry/coordination.db`
  (`claims` + `findings`, busy_timeout, WAL, current schema only), lazily
  created and NEVER created by a read — a project without `.sherry/` answers
  empty. Board content never enters the system prompt (it changes on every
  write, so it would break the prefix cache); it moves only through tool calls.
  A functional role's `tools:` list is an allow-list, so the specialized
  definitions name the tools — a child would otherwise never see them however
  the main agent is configured (pinned by `tests/agent/tools/test_coordination.py`).
  The executor, the only writer, names all three (it is the only role that
  claims a path); `researcher` / `reviewer` / `librarian` name only the
  sharing/reading pair, so a read-only role never expresses write intent. Both
  switches off ⇒ `build_main_tools()`, the child tool face and
  `GET /agent/catalog` are byte-identical to a build without the feature.
- **The SUBAGENT lane sits behind a load gate**
  (`agent/tools/pub_base/load_gate.py`, `LANE_SYSTEM["load_gate_*"]`). The lane
  is COUNTING backpressure ("are all 8 slots busy"); the gate is the
  environment half ("can the machine carry one more"). It runs BEFORE
  `lane_slot(SUBAGENT)` in `_execute_subagent_with_lane`, so a deferred dispatch
  holds no slot: at ≥90% CPU/RAM it only logs (the cap throttles there), at
  ≥95% it defers — the run stays PENDING, ONE `load_warning` frame goes to the
  session (module-level per-run throttle), and it re-checks after
  `load_gate_retry_s`. The deferral is BOUNDED: past `load_gate_max_defer_s`
  (600 s) the run is terminalized via `complete_run(TIMEOUT, error="load_timeout:
  …")` rather than abandoned, because an abandoned PENDING run is an orphan the
  sweeper would re-dispatch → defer again (a loop); a kill during the wait is
  seen through `kill_reconciliation` (a killed PENDING run is NOT TERMINAL) and
  ends it at once. Sampling (three CPU intervals ≈4.5 s) runs in
  `asyncio.to_thread` at most once per `load_gate_snapshot_ttl_s` (5 s) for the
  whole process — concurrent waiters share one verdict — and FAILS OPEN on a
  psutil error (a broken monitor must not stall dispatch) while honouring a real
  stop. Shipped `load_gate_enabled: False` (byte-for-byte the old behavior);
  enabling it requires the packaging smoke step that proves psutil reached the
  frozen tree (`HIDDEN_IMPORTS` in `packaging/build_backend.py`), because the
  import is lazy and invisible to the freezer.
- **Sherry's own files are gated by two allow-lists**
  (`agent/tools/pub_base/self_path_policy.py`). Nothing configured means the
  session root IS the checkout, so the in-root test used to call the whole
  source tree — and the credentials beside it — safe; a WHITELIST replaces that
  assumption. WRITE allows only `<project>/.sherry/`, the CURRENT session's
  `plans|attachments|uploads`, `TEMP_DIR`, `STATIC_DIR`,
  `SRC_DIR/{images,audio,video}` and `SRC_DIR/rag/uploads`. READ is broader by
  design — the whole source tree is readable (reading breaks nothing, leaks
  nothing) — and denies `.env` `.models` `.network` `sherry.jsonc` and their
  `.example`s, `src/data/**`, `src/checkpoints/**`, the workspace persona files
  and `workspace/{memory,knowledge}/**`, model-weight suffixes, `mcp_servers/**`,
  the writable skills tree, `CODE_INTEL_DIR`, `LOG_DIR`, the shared trajectory
  ledger and every session directory except the CURRENT session's `plans/`.
  Enforced in `resolve_tool_path` (including its IN-ROOT success branch, and
  `search_files`' own resolve) and in `resolve_external_path`; a path in a
  DIFFERENT session project directory is the user's workspace and never reaches
  it. The main agent gets one-shot HITL (`sherry_self_access`, approve/reject
  only — no YOLO, no session allowlist), a SUBAGENT is refused with no card,
  and outside an agent run (no runnable context to ask) the access fails CLOSED.
  Internal writers are untouched: `memory`, `skill_manage`, `exit_plan_mode`,
  the ledger, shadow-git, the checkpointer, trajectory and the knowledge index
  build paths from `config.path` constants and never call those resolvers.
- An opt-in `sessions_spawn(isolation=True)` gives a child its own GIT WORKTREE of the project (`agent/tools/subagent/isolation/`), cut from a dirty baseline (`git stash create`, so uncommitted edits are visible; untracked files are copied in, ignored paths materialized as links/copies per `SUBAGENT_ISOLATION`, and a non-repository project is auto-initialized with a deny-listed baseline commit) whose changed files merge back at announce time under a per-root `flock` with the same revision CAS — conflicts are reported by path and left untouched, symlinks/ignored caches never merge, and every merged code file is symbol-diffed before it is written so the completion reply carries an **Interface changes** section (renamed/added/removed functions, methods, classes — `agent/tools/code_intel/symbol_diff.py`, gated by `SUBAGENT_ISOLATION["interface_diff_enabled"]`, fail-open and capped; the diff ALSO reports `changed:` entries for a surviving name whose parameter/return shape moved, and surfaces module-level `variable` symbols — both are opt-in to this diff, so the code index still lists only functions/methods/classes), the section then names `affected importers` from a READ-ONLY reverse lookup over the code index's `import_edges` (`code_intel/indexer.py::importers_of_names`; a missing index answers empty and the merge NEVER builds one) and those files are marked stale in the parent's evidence ledger alongside the merged paths (a pure import user has no write to match otherwise), with an optional `EVIDENCE_REGRESSION_GATE["merge_reverify_enabled"]` (default OFF, `merge_reverify_max_commands` 5) replaying the parent's passed commands that cover them; it is ONE LEVEL: the runtime `sessions_spawn` tool carries `isolation`, and a spawn whose cwd already sits inside an isolated worktree reuses that tree (`isolation=True` is ignored there, info-logged, never refused — a child of the isolated ORCHESTRATOR works in the same tree, and its changes ride that one merge back; the check is a path test against `isolated_workspaces_dir()`, not a git probe); the docs page is `wiki/file-safety/README.md`. A workspace no live run owns is cleaned by the stale-workspace sweep (`agent/tools/subagent/isolation/sweep.py`, every sweeper cycle and once at boot; conflict keeps expire after `SUBAGENT_ISOLATION` `stale_workspace_ttl_days`).
- a revert never touches the conversation: it appends one `<revert>user had revert editing …</revert>` AIMessage per revert (an `aupdate_state` write the persistence layer mirrors into the store), and the agent reads what was undone on its next turn.
- the todo-continuation middleware is named `PlanContinuationEnforcer` (it supervises the plan = todos + TaskFlow flows), and TWO strings are deliberately frozen: `message.metadata["origin"]` stays `"todo_continuation"` (historical rows keep resolving) and `agent_switch._RENAMED_MIDDLEWARES` maps `PlanContinuationEnforcer → {TodoContinuationEnforcer}` so a preset payload written before the rename still switches it off. Its turn-end hook also counts turns without a `todowrite` (`stagnation_tracker.record_turn` / `record_todowrite`, threshold `TODOLIST_INFRA["todo_write_nudge_turns"]` = 10) and APPENDS a `[REMINDER: TODO TRACKING]` nudge to the continuation directive once it is crossed — the one shape the snapshot-based stagnation check cannot see (an agent advancing TaskFlow clears the streak while the todo list rots). Recovery mode carries its own todowrite guidance, so the nudge stays out of that branch. The same blind spot (a plan edited just enough to keep the snapshot moving) is bounded by the continuation budget: `TODOLIST_INFRA["max_continuation_turns"]` (15) confirmed deliveries → one stop directive + a session latch that only a turn the gate did NOT inject clears. A completed plan (no incomplete todo, no open flow) earns ONE `[SYSTEM DIRECTIVE: FINAL VERIFICATION CHECK]` auto-turn before the gate treats it as done: the paranoid prompt asks the agent to re-read the diff, actually run the tests/typecheck/lint and launch user-facing changes. It is a PROMPT, not a gate (no evidence query, no HITL, no schema); a process-local fingerprint throttles it to once per completed plan, a changed plan re-arms it, a refused delivery is retried, and a TaskFlow session skips it (its finish gates own verification).
- todo identity is `todo_id`, not `position`: each row gets a per-session monotonic id, the system reminder renders it as `#N`, and `todowrite` actions `patch` / `add(after_todo_id=…)` / `remove` / `reattach` / `list_orphans` address it. `replace` is still the default full-list rewrite and reissues every id (it reads `MAX(todo_id)` before DELETE, so an old id can never silently point at a new row); local actions preserve `flow_id` / `step_id` / `plan_ref`. A missing id raises `TodoIdNotFoundError` and the tool answers with the current valid list. The store creates only the current schema (`todo_id INTEGER NOT NULL`, unique `(session_id, todo_id)` index), with no old-table backfill; `position` is display order only and insertion shifts it via `TODOLIST_INFRA["position_shift_offset"]`.
- Todo↔TaskFlow reference integrity is enforced at four points: (1) a full `todowrite` replacement that drops a flow's GC-root todo runs `compensate_orphaned_flows` immediately (`TODOLIST_INFRA["orphan_flow_on_drop"]`: `cancel` default, `refuse` rejects the rewrite, `sweep_only` leaves it to the sweep), kills the flow's live detached children (fail-open) and the tool result carries a `[REFERENCE INTEGRITY]` warning; (2) one session-scoped sweep follows the replacement with the CONFIGURED grace window (a just-completed root on its way to `taskflow_flow(action="finish")` is not collected; the targeted compensation already handled the dropped flows); (3) `taskflow_flow(action="create")` / `taskflow_flow(action="plan")` answer a similar active same-session flow with a reuse-or-`ack_dup=True` warning (normalized substring or Jaccard > 0.7; `resumed_from` skips it; a terminal flow never counts); (4) a terminal flow notifies its root at once — `completed` on finish, `cancelled` on fail/cancel — while the sweeper's delayed cancel remains the backstop. All four are fail-open; both notification helpers update only non-terminal rows (idempotent).
- a middleware row in 预设角色-中间件 must never let its label squeeze the switch: PrimeVue's `ToggleSwitch` cannot take a fall-through class from a fragment root, so each row wraps it in a `span.shrink-0` (the label column keeps `min-w-0 truncate`) and the row's help affordance is a real `?` button that toggles the explanation inline (hover tooltips are unreachable in an embedded webview).
- the `<system-reminder>` layer's contract lives in `pub/func/message/system_reminder.py` (`provenance="reminder_catalog"` + `internal=True` + the source id as `origin`), mirroring the workspace-notice leaf so the scanners that walk human messages can skip both: `split_into_turns` (a reminder is not a turn boundary), HITL's headless-turn check and the summarizer's serializer (`[System notice]` / `system`, never `[User]`). Wrapping escapes nested tags — todo text is user-influenced.
- user-configurable lifecycle hooks (`agent/middlewares/user_hooks/`, registered
  after `ToolResourceLockMiddleware`, which itself sits right after
  `ToolGuardrails` — a guardrail-BLOCKED call never reaches a hook):
  the `sherry.jsonc` `hooks` section names commands per event and the middleware
  dispatches them as SUBPROCESSES with the turn's identity. Step 1 only —
  `PostToolUse` / `PostToolUseFailure` (result/error previews bounded by
  `USER_HOOKS["*_preview_chars"]`, `durationMs` measured right here) and `Stop`
  (`responsePreview` + `toolCallCount`); `PreToolUse`, `UserPromptSubmit`,
  `SessionStart` and the `updatedInput` rewrite are NOT dispatched (the config
  parser still validates them, and the constructor logs a warning naming each
  ignored event once) because blocking/rewriting hooks need a trust gate this
  build does not have. The dispatcher NEVER blocks a turn: `shlex.split` and no
  shell, a scrubbed env (`scrub_env` + `SHERRY_SESSION_ID`/`SHERRY_HOOK_EVENT`),
  a per-hook timeout that kills the process, stdout capped at
  `USER_HOOKS["max_output_bytes"]` and parsed as OPTIONAL outcome JSON (non-JSON
  = success, and `decision`/`updatedInput` are parsed but ignored), stderr
  logged (capped) and never shown to the model, and every failure is a log line
  — the tool result rides through untouched. `hooks.enabled` defaults OFF
  (`USER_HOOKS["enabled"]`): the config file is git-tracked, so executing its
  commands must be a deliberate act. Added to `MIDDLEWARE_ORDER` +
  `GATEABLE_MIDDLEWARES` (the 预设-中间件 switch calls `middleware_enabled`), and
  the config is read once at construction (change = restart).
- the TaskFlow loop closes itself: a child spawned by a flow resumes it
  automatically (`agent/tools/subagent/announce/delivery.py::_try_auto_resume_taskflow`
  finds the owning flow by scanning active flows' steps for the child key), and
  `taskflow_step(action="resume")` then dispatches the wave it unlocked
  (`auto_dispatch=True` default; `auto_dispatch=False` keeps the "unlock only"
  shape). The AI still supervises the exceptions — judge BLOCK, failures,
  planning and the final finish — instead of driving every step. `taskflow_flow(action="replan")`
  regenerates a flow's REMAINING steps from the goal, the done results and the
  failure reason (transitively invalidated steps go back to `blocked`; bounded by
  `REPLAN_MAX` replans per flow), `taskflow_flow(action="plan")` decomposes a goal into a
  dependency-ordered flow without dispatching anything, and `taskflow_flow(action="finish")`
  gained **Gate E** (`_goal_gate`): with a flow description present, an auxiliary
  model judges whether the collected results actually satisfy the goal (fail-open;
  a rejection names `taskflow_flow(action="replan")`).
- TaskFlow dispatch is **serialized per flow** and planning is idempotent:
  `_dispatch.flow_dispatch_lock(flow_id)` (process-local, keyed by loop + flow)
  wraps the manual `taskflow_step(action="dispatch")` read → validate → spawn → persist body
  and the resume auto-dispatch wave, which re-checks each step is still `ready`
  after acquiring it — two dispatchers cannot spawn the same step twice. A
  multi-process deployment would need a DB lease. `taskflow_flow(action="plan")` generates
  `plan-<sha256(session_id + goal)[:10]>` (stable across processes, session
  scoped because `flow_id` is a global PK); repeating a goal/session returns
  the existing non-terminal flow as an idempotent success, and a duplicate
  non-terminal `taskflow_flow(action="create")` does the same (terminal flows still error).
  A new signature-based tool-loop middleware was **not** added: 204 historical
  tool calls showed zero consecutive same-name + same-args successful mutating
  runs; `ToolGuardrails` remains the detector.
- TaskFlow orphan collection is an **unreferenced-flow GC plus liveness** (v3),
  replacing the per-scenario fixes: every
  `taskflow_flow(action="create")`/`taskflow_flow(action="plan")` binds a
  flow-level `pending` todo (the GC root; `step_id` empty, so it stays active
  until the whole flow is terminal) — a missing `todo_content` falls back to the
  description/flow id rather than creating an unrooted flow. The sweeper marks
  active-todo `flow_id`s as the reachable set and fails non-terminal,
  non-superseded flows with no root and an `updated_at` older than
  `TASKFLOW_INFRA["unreferenced_flow_grace_seconds"]` (300 s — the grace covers
  the create/bind race between the two SQLite files) as `orphan_unreferenced`.
  Liveness then handles stuck-but-referenced flows: a child-session flow with
  no live run is collected after the short grace, a main-session flow is
  collected after `stale_action_hours` (12 h) of inactivity
  (`session_dead` when no socket/turn/run exists, `stale_swept` when the
  session is live), a live child vetoes, and active todos referencing a
  failed/cancelled flow are cancelled. Both passes run at boot (before the
  first 60 s tick) and every tick; `task_flows` carries `updated_at`
  (bumped by every mutation) and `terminal_reason` columns. A superseded
  predecessor becomes `cancelled`/`superseded`; `taskflow_flow(action="finish")` has three
  independent budgets (nudge / consecutive failures / protocol violations)
  that auto-fail and record detached children; terminal rows are pruned to
  `max_total_flows_per_session` (200) on a debounced post-mutation pass.
- a dispatch wave spawns CONCURRENTLY, bounded by
  `TASKFLOW_INFRA["max_parallel_dispatch"]` (2; 1 restores the historical
  sequential behaviour) through `asyncio.gather` + a semaphore — the three
  dispatch loops (`taskflow_step(action="dispatch")`'s batch, `taskflow_step(action="resume")`'s
  auto-dispatch wave and `taskflow_step(action="wait_all")`'s retry pass) all do. The SUBAGENT
  lane is the global backpressure; this cap only decides how many spawn calls
  are in flight at once. A partial wave failure persists every successful child
  (via `update_flow_with_conflict_retry`, so a concurrent writer cannot drop a
  spawned key) and names the failed step ids (`auto_dispatch_failed=…` on the
  resume answer). `taskflow_step(action="resume")`'s normal path also writes through that
  CAS-retry: the `build_state` callback re-applies the ALREADY-DECIDED verdict +
  result onto the freshly-read state and re-derives the unlock set from what was
  actually persisted — two children completing at once both land, and the same
  step is spawned exactly once. A superseded flow releases its per-session slot.
- two nullable `task_flows` columns record flow lineage (`resumed_from` /
  `superseded_by`): `taskflow_flow(action="create", resumed_from=…)` —
  the CREATE path, since `taskflow_flow(action="replan")` rewrites in place and never forks —
  writes the successor's forward pointer and stamps the predecessor's
  back-pointer with a plain session-scoped UPDATE that deliberately does NOT bump
  `expected_revision` (lineage is metadata). `get_active_flows_sync` excludes a
  superseded flow even while it is still `running`; `get_all_flows_sync(all)`
  and `taskflow_inspect(action="summary")` still show it.
- `_try_auto_resume_taskflow` only reports success when the resume answer is not
  an `Error:` (the resume genuinely failed → an error log, not an info log; the
  child's completion is still recorded, so nothing is lost).
- tool duration is measured ONCE, at tool-return time, by `message_persistence`'s `wrap_tool_call`/`awrap_tool_call`
  (`time.monotonic()` difference, rounded, clamped at 0 — never a wall-clock subtraction: an NTP step back would make it
  negative, which is the defect ZCode ships unguarded), stamped into `ToolMessage.additional_kwargs["tool_duration_ms"]`
  and persisted to `messages.tool_duration_ms` (a row without the stamp reads NULL). The stream's
  `tool_result.duration_ms` is the same measurement for the live card (`stream_dispatch`'s `_tool_started_at`, per
  session + tool id, consumed once). Approval waits and queue time are OUTSIDE the window by construction — the clock
  starts when the tool actually executes. The client shows a live ticker while running and the measured value when
  settled (`formatToolDuration`: <1s → `850ms`, <10s → `1.2s`, else whole seconds), and nothing when unknown.
- the code-intelligence index follows the SESSION's project directory
  (`agent/tools/code_intel/roots.py::index_db_for_root` → `<root>/.sherry/codeintel/index.db`),
  so alternating between projects no longer rebuilds one shared index (the stored
  paths are project-relative); `code_intel_index_db_path` pins one and
  `SHERRY_CODE_INTEL_DB` overrides the path, with the shared `CODE_INTEL_DIR`
  as the no-root fallback. The index root itself is resolved PER CALL
  (env override > session project dir > process default), like ast-grep and LSP
- the code-intel semantic layer is OPTIONAL and CODE-EMBEDDER-ONLY: BGE remains
  the text embedder (`context_engine` / multimodal RAG) and is never used here.
  `CODE_INTEL_SEMANTIC["enabled"]` gates registration; without a valid profile
  (`jina-embeddings-v2-base-code`, ONNX, 768d, Apache-2.0, under
  `models/embed_model/code/`) the tool is absent and no model/index is touched.
  The Jina backend is lazy (`onnxruntime` + `RobertaTokenizerFast`, mean pooling);
  switching profiles purges the old `code_embeddings` rows, including legacy
  `bge-m3` rows, so no BGE vector can be scored. It is OFF by default: merely
  having the files is not enough — the 模型配置 panel downloads the model and
  writes the opt-in marker (`GET /code-embedder/status`, `POST /code-embedder/download`,
  `DELETE /code-embedder` disables; the download runs in a background thread).
- truncation is budgeted in TOKENS, CJK-aware (`pub/func/message/token_truncation.py`):
  tool outputs, tool args, the summary body, the aggressive pass and every
  SummaryDoc list item go through `truncate_by_tokens` (`evicted_refs` is exempt —
  a clipped path is useless), so the same budget costs the same in every language
  (the old character caps ÷ 4 keep English byte-identical)
- two boot sweeps reconcile crash leftovers, both conservative and both run from
  the server assembly (`server/trigger/channels/core.py::_schedule_sweeper`, next
  to the sweeper start): `agent/tools/subagent/isolation/sweep.py` removes
  isolated workspaces no REGISTRY run owns (orphaned / expired-conflict past
  `SUBAGENT_ISOLATION["stale_workspace_ttl_days"]` / corrupted) plus a
  `git worktree prune` on their parents, and
  `server/service/session_dir_sweep.py` removes `SESSIONS_DIR/<id>/` folders NO
  store references (messages / todos / taskflows / checkpoints all probed,
  fail-open per store; symlinks and folders younger than
  `SESSION_DIRS["min_dir_age_seconds"]` are skipped)
- `.gitignore` line `*.db` ignores all SQLite files — DB files are never committed
- pre-push hook runs basedpyright on the entire diff — must be 0 errors before push
