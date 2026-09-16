# AGENTS.md — sherry_agent

AI coding assistant guide for the EMA AI Agent (Sherry) project. Read this before exploring the codebase.

## Project

**EMA AI Agent** — a role-playing AI agent with long-term memory, multi-level subagents, and a task orchestration engine. Python 3.13 (uv) · LangChain/LangGraph 1.3 · Robyn backend · Nuxt 4 + Tauri 2 frontend · SQLite (WAL) storage.

## Quick Commands

```bash
uv run python -m server                              # start backend (127.0.0.1:8080)
uv run pytest tests/agent/middlewares -q -k "not llm_e2e"  # run middleware tests
uv run --no-sync python tests/run_tests_split.py     # CI test gate (3-process)
uv run --no-sync lint-imports                        # import-linter contract check
uv run --with ruff ruff check . && uv run --with ruff ruff format --check .  # lint + format
uv run --no-sync basedpyright agent/                 # type check
cd client && pnpm test:unit && pnpm test:integration && pnpm run dpdm  # frontend tests
```

## Directory Structure

| Directory | Purpose | Key entry point |
|---|---|---|
| `agent/` | Agent core: middleware chain, tools, subagent system, checkpointer | `agent/core.py::built_agent()` |
| `agent/middlewares/` | Middleware pipeline (summarization, guardrails, HITL, intent, continuation, memory_flush) | `agent/middlewares/__init__.py` |
| `agent/tools/` | LLM-callable tools (taskflow, todolist, memory, subagent, file, search, ...) | `agent/tools/__init__.py::build_main_tools()` |
| `agent/tools/taskflow/` | Task orchestration engine (DAG, budget, deadline, progress, board) | `agent/tools/taskflow/config.py` |
| `agent/tools/todolist/` | Session-scoped todo planning layer | `agent/tools/todolist/service.py` |
| `agent/tools/memory_tiered.py` | LT-1 tiered facts store (facts/*.md) | `get_tiered_store()` |
| `agent/tools/subagent/` | Multi-level subagent system (spawn/registry/announce/sweeper) | `agent/tools/subagent/spawn/core.py` |
| `agent/wrapper/` | Graph-level wrappers (repetition guard, context limit) + pluggable registry | `agent/wrapper/registry.py` |
| `config/` | Centralized configuration (paths, features TypedDicts, schema, settings) | `config/__init__.py` |
| `config/features/` | Per-object feature config (35 TypedDicts) | `config/features/__init__.py` |
| `server/` | Robyn HTTP/WS backend (trigger → service → queue/DAO → utils) | `server/__main__.py` |
| `context_engine/` | Memory engine (MesMemory SQLite + curator) | `context_engine/store/db.py` |
| `workspace/` | Live persona files (gitignored; templates in `workspace/template/`) | `workspace/prompt_builder.py::build_system_prompt()` |
| `pub/` | Shared utilities (message pipeline, retry, validators) | `pub/func/message/` |
| `models/` | LLM/model wrappers (main, reasoner, auxiliary, vision, embed, reranker) | `models/LLMs/main_llm.py` |
| `runtime/` | Runtime state registers (`session/`) + process-level services (`process/`, `lane/`) + dependency-inversion seams (`hooks.py`, `data_provider.py`) | `runtime/session/state_register.py` |
| `runtime/hooks.py` | Process-level callback registry — `agent`/`skills` resolve server-owned callables (auto-turn trigger, WS task table, skill scan) without importing `server` | `runtime/hooks.py` |
| `runtime/data_provider.py` | Prompt/skill-write provider registries (`PromptDataProvider`/`SkillWriteProvider`) — `workspace`/`context_engine` obtain agent-owned data without importing `agent` | `runtime/data_provider.py` |
| `runtime/lane/` | Process-level concurrency lanes (`Lane`/`LaneManager`/`lane_slot`/`LaneType`) | `runtime/lane/core.py` |
| `tests/` | Mirror-structured pytest suite (markers: unit/integration/module/system/regression) | `tests/run_tests_split.py` |
| `skills/` | SKILL.md skill system (builtin/auto/plugins) | `skills/loader.py::scan_skills()` |
| `docs/` | VitePress documentation site | `docs/long-running-tasks/README.md` |
| `scripts/` | Dev tooling (git hooks, maintenance scripts) | `scripts/hooks/` |

## Architecture

```
User message → Robyn WS → agent.core.built_agent() graph
  │
  ├─ middleware chain (before_agent → before_model → LLM → tools → after_model → after_agent)
  │    ContextEngineHook → MultimodalProcessor → IterationBudget → ToolGuardrails
  │    → ToolCallNormalize → SubagentCompletionDrain → TaskIntent(E7) → OutputRepetitionGuard
  │    → MaxTokensBoost → HeartbeatStaleness → HITL → LLMRetry → Summarization
  │    → TodoContinuationEnforcer(E3)
  │
  ├─ tools: build_main_tools() → taskflow(13) + todolist(2) + memory + subagent(7)
  │         + file_tools + web_search + terminal + python_repl + question + ...
  │
  ├─ graph wrappers: apply_graph_wrappers(inner)
  │    → ContextLimitGuard(RepetitionGuard(graph))
  │
  ├─ lanes: runtime/lane/lane_slot() gates every dispatch point —
  │    MAIN(turn) / SUBAGENT(child) / NUDGE(memory) / NESTED(sessions.send)
  │
  └─ subagents: spawn_subagent_direct() → accepted as PENDING when the
       SUBAGENT lane is full → RUNNING inside the lane slot (`started_at`
       stamped there) → detached child agents → announce pipeline → drain
```

## Concurrency Lanes (`runtime/lane/`)

Four process-level lanes, each an `asyncio.Semaphore` + active/queued counters, gate concurrent work instead of rejecting it: over-limit work waits FIFO.

| Lane | Constrains | Default | Config key |
|---|---|---|---|
| `MAIN` | main-agent turn (`_run_executor`) | `min(16, max(8, CPU))`, clamped up to `SUBAGENT + NUDGE` (12) → 12–16 | `LANE_SYSTEM["main_max_concurrent"]` |
| `SUBAGENT` | child-agent executions (spawn + steer) | 8 | `LANE_SYSTEM["subagent_max_concurrent"]` |
| `NUDGE` | nudge/persistence calls (`nudge.py`, 3 sites) | 4 | `LANE_SYSTEM["nudge_max_concurrent"]` |
| `NESTED` | `sessions_send` reply turns (serial) | 1 | `LANE_SYSTEM["nested_max_concurrent"]` |

- Config: `config/features/infra_side/lane_system.py`; `validate_lane_config()` runs at server startup and enforces `main >= subagent + nudge` (all limits ≥ 1); `install_lane_lifecycle()` in `server/service/lane_lifecycle.py` validates, prewarms the manager, registers `set_drain_check(is_gateway_draining)`, and installs a bounded exit drain (`atexit`, `drain_all(timeout=0)` — never blocks exit).
- Queueing: a spawn that passes per-parent admission but exceeds the SUBAGENT lane is registered `PENDING` (no `forbidden`); `PENDING → RUNNING` happens inside the lane slot and only then is `started_at` stamped (queue wait is not run time). `PENDING` counts as active in registry queries and is killable; orphan recovery finalizes a PENDING run that lost its task with `ended_reason="pending_orphaned"`.
- Drain mode: `set_drain_check(fn)` makes `Lane.acquire()` refuse (without consuming a permit) while the subagent gateway reports draining.
- Observability: `GET /lane-status` → `{main|subagent|nudge|nested: {name, max_concurrent, active, queued}}`.
- Hot updates (`LaneManager.set_concurrency`) only affect new acquires; in-flight slots keep their permits.

## Key Configuration

| File | Contents |
|---|---|
| `config/features/agent_side/` | 20 per-object TypedDicts (summarization, guardrails, iteration, memory_flush, tiered_memory, taskflow_infra, todolist_infra, tools_timeouts, ...) |
| `config/features/infra_side/` | 18 per-object TypedDicts (gateway, bus, http_upload, retry_backoff, server_http, ws_stream, input_queue, heartbeat, cron, skill_scanner, mes_memory, curator, model_pricing, ...) |
| `config/features/__init__.py` | Aggregator — all 38 TypedDicts + instances re-exported |
| `config/path.py` | All filesystem paths (ROOT_DIR, SKILLS_DIR, WORKSPACE_DIR, FACTS_DIR, ...) |
| `config/schema.py` | Pydantic Config (SHERRY_ env prefix, mostly unused at runtime) |
| `config/sherry_settings.py` | sherry.jsonc loader (TOOL_CALL_TIMEOUT_MINUTES, LOG_LEVEL, curator.*, LANGSMITH.*) |

## Code Graph

CodeGraph MCP (`@colbymchenry/codegraph`, wired in `opencode.json`) indexes the repo into `.codegraph/codegraph.db` (gitignored) — a 20-language AST knowledge graph (Python/TS/Rust full; `.vue` gets no AST). One MCP tool, `codegraph_explore`, returns verbatim source + call paths (dynamic dispatch included) + blast radius. Auto-syncs on file events (2s debounce). CLI: `codegraph query|explore|callers|impact|status`. Rebuild index after big restructures: `codegraph init`. It is a snapshot — verify against source when editing.

## Import Rules

- `server/**` layering: trigger → service → queue|DAO → utils (enforced by import-linter)
- Cross-package bans (enforced by `uv run --no-sync lint-imports`, `[tool.importlinter]` in `pyproject.toml`; grimp counts function-level imports, so a lazy import does NOT satisfy them):
  - `agent/**` MUST NOT import `server/**`
  - `config/**` MUST NOT import `models/**`
  - `context_engine/**` MUST NOT import `agent/**`
  - `workspace/**` MUST NOT import `agent/**` or `context_engine/**`
  - `skills/**` MUST NOT import `server/**`
- Cross-boundary seams: `runtime/hooks.py` (callback registry) and `runtime/data_provider.py` (`PromptDataProvider`/`SkillWriteProvider`) are leaf modules importable from both sides — owners register at assembly time (server boot / `agent.core.init()`), consumers resolve at call time. Never reintroduce a direct import to cross a forbidden boundary.
- `config/features/**` MUST NOT import from `agent/`, `server/`, or `models/` — config is dependency-free
- `config/**` is importable from ALL layers (no restriction)
- `config/num.py` is DELETED — all constants live in `config/features/agent_side/summarization.py` (SUMMARIZATION TypedDict) and other per-object modules

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

**IMPORTANT**: `tests/agent/tools/subagent/conftest.py` installs sys.modules stubs at collection time. When running multiple test dirs in one process, use the stub-tolerant loading pattern from `tests/agent/tools/taskflow/conftest.py`. The split runner (`tests/run_tests_split.py`) avoids this by running groups in separate processes.

## Conventions

- **Python 3.13** (StrEnum, PEP 695 type aliases, typing.override)
- **Config**: one TypedDict + one instance per feature in `config/features/<side>/<name>.py`, re-exported via `__init__.py` — NEVER use bare dict literals for feature config
- **Middleware hooks**: `abefore_model(self, state, runtime=None)`, `aafter_agent(self, state, runtime=None)` — LangChain 1.3.9 signatures; after_agent runs in REVERSE list order
- **Tool registration**: `@tool("name")` + `build_*_tools()` factory + `_MAIN_TOOLS_BUILDERS` list in `agent/tools/__init__.py`
- **Test fixtures**: `isolated_db` (tmp SQLite), `build_main_tools_real` (stub-tolerant), `scan_skills_real`
- **Commits**: angular conventional (commitlint enforced), pathspec-only (`git commit -- <files>`)
- **File naming**: snake_case enforced by pre-commit hook
- **Line length**: 100 chars (ruff)

## Known Pitfalls

- `workspace/` in `.gitignore` is anchored (`/workspace/`) — live persona files are NOT tracked
- `tests/agent/tools/subagent/conftest.py` pollutes `sys.modules` globally — use stub-tolerant imports in cross-suite tests
- `agent/core.py::init()` imports `skills` lazily and calls `build_skills_snapshot()` at assembly time (server boot); a process that never calls `init()` still gets a live disk scan from `scan_skills(use_cache=True)`
- `Summarization` has both sync (`_apply_compression`) and async (`_aapply_compression`) paths — changes must cover both
- `taskflow_resume` and `taskflow_run_task` both dispatch via `_dispatch.dispatch_child` — the seam is monkeypatchable
- after_agent hooks run in REVERSE list order — first registered = last executed
- `workspace/memory/facts/` is created at runtime by TieredMemoryStore — gitignored
- `asyncio.Semaphore` is event-loop-bound, so lanes must be acquired on the main loop — a cross-loop `acquire()` logs a warning and rebinds a fresh semaphore with outstanding slots deducted (never double-issues permits)
- `.gitignore` line `*.db` ignores all SQLite files — DB files are never committed
- pre-push hook runs basedpyright on the entire diff — must be 0 errors before push
