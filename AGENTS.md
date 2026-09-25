# AGENTS.md — sherry_agent

AI coding assistant guide for the EMA AI Agent (Sherry) project. Read this before exploring the codebase.

## Project

**EMA AI Agent** — a role-playing AI agent with long-term memory, multi-level subagents, and a task orchestration engine. Python 3.13 (uv) · LangChain/LangGraph 1.3 · Robyn backend · Nuxt 4 + Tauri 2 frontend · SQLite (WAL) storage.

## Quick Commands

```bash
uv run python -m server                              # start backend (127.0.0.1:8080)
uv run pytest tests/agent/middlewares -q -k "not llm_e2e"  # run middleware tests
uv run --no-sync python tests/run_tests_split.py     # CI test gate (3-process)
uv run --no-sync python scripts/check_docs_parity.py # four-language README parity gate
uv run --no-sync python scripts/check_doc_links.py   # Markdown dead-link + anchor gate
uv run --no-sync lint-imports                        # import-linter contract check
uv run --with ruff ruff check . && uv run --with ruff ruff format --check .  # lint + format
uv run --no-sync basedpyright agent/                 # type check
cd client && pnpm test:unit && pnpm test:integration && pnpm run dpdm  # frontend tests
```

## Directory Structure

| Directory | Purpose | Key entry point |
|---|---|---|
| `agent/` | Agent core: middleware chain, tools, subagent system, checkpointer | `agent/core.py::built_agent()` |
| `agent/middlewares/` | Middleware pipeline (summarization, guardrails, HITL, intent, continuation, memory_flush, message_persistence) | `agent/middlewares/__init__.py` |
| `agent/tools/` | LLM-callable tools (taskflow, todolist, memory, subagent, file, search, ...) | `agent/tools/__init__.py::build_main_tools()` |
| `agent/tools/taskflow/` | Task orchestration engine (DAG, budget, deadline, progress, board, step judge, evidence collector) | `agent/tools/taskflow/config.py` |
| `agent/tools/todolist/` | Session-scoped todo planning layer + append-only evidence ledger/recorder | `agent/tools/todolist/service.py` |
| `agent/tools/subagent/` | Multi-level subagent system (spawn/registry/announce/sweeper, completion judge, functional roles) | `agent/tools/subagent/spawn/core.py` |
| `agent/tools/pub_base/` | Shared tool infrastructure (`BaseSQLiteRepository` for the three SQLite stores, path utils, skill usage) | `agent/tools/pub_base/sqlite_store.py` |
| `agent/wrapper/` | Graph-level wrappers (repetition guard, context limit) + pluggable registry | `agent/wrapper/registry.py` |
| `config/` | Centralized configuration (paths, features TypedDicts, schema, settings) | `config/__init__.py` |
| `config/features/` | Per-object feature config (51 TypedDicts) | `config/features/__init__.py` |
| `server/` | Robyn HTTP/WS backend (trigger → service → queue/DAO → utils) | `server/__main__.py` |
| `context_engine/` | Memory engine (MesMemory SQLite + curator) | `context_engine/store/db.py` |
| `workspace/` | Live persona files (gitignored; templates in `workspace/template/`) | `workspace/prompt_builder.py::build_system_prompt()` |
| `pub/` | Shared utilities (message pipeline, retry, validators) | `pub/func/message/` |
| `models/` | LLM/model wrappers (main, reasoner, auxiliary, vision, embed, reranker) | `models/LLMs/main_llm.py` |
| `runtime/` | Runtime state registers (`session/`; canonical keys in `state_keys.py`) + process-level services (`process/`, `lane/`) + dependency-inversion seams (`hooks.py`, `data_provider.py`) | `runtime/session/state_register.py` |
| `runtime/hooks.py` | Process-level callback registry — `agent`/`skills` resolve server-owned callables (auto-turn trigger, WS task table, skill scan) without importing `server` | `runtime/hooks.py` |
| `runtime/data_provider.py` | Prompt/skill-write provider registries (`PromptDataProvider`/`SkillWriteProvider`) — `workspace`/`context_engine` obtain agent-owned data without importing `agent` | `runtime/data_provider.py` |
| `runtime/lane/` | Process-level concurrency lanes (`Lane`/`LaneManager`/`lane_slot`/`LaneType`) | `runtime/lane/core.py` |
| `tests/` | Mirror-structured pytest suite (markers: unit/integration/module/system/regression) | `tests/run_tests_split.py` |
| `skills/` | SKILL.md skill system (builtin/auto/plugins) | `skills/loader.py::scan_skills()` |
| `docs/` | VitePress documentation site | `docs/long-running-tasks/README.md` |
| `docs/context-governance/` | Context governance docs (persistence, eviction, slices, overflow clip, summary filtering) | `docs/context-governance/README.md` |
| `scripts/` | Dev tooling (git hooks, maintenance scripts) | `scripts/hooks/` |

## Architecture

```
User message → Robyn WS → agent.core.built_agent() graph
  │
  ├─ middleware chain (before_agent → before_model → LLM → tools → after_model → after_agent)
  │    system_prompt_injection (@dynamic_prompt) → MultimodalProcessor → IterationBudget → ToolGuardrails
  │    → ContextEviction(P0-2/P2-4) → ToolCallNormalize → PathGuard → SubagentCompletionDrain → TaskIntent(E7)
  │    → OutputRepetitionGuard → MaxTokensBoost → ThinkingControl → HeartbeatStaleness → HITL → MessagePersistence
  │    → LLMRetry → Summarization → TodoContinuationEnforcer(E3)
  │    (MessagePersistence flushes tool results the moment they return via
  │     wrap_tool_call; after_model nodes chain in reverse registration order, so it
  │     is also the first after_model hook — new human/ai/tool messages reach
  │     MesMemory before HITL rewrites denials or interrupts. HITL denials are the
  │     exception: its short-circuit bypasses the wrap layer and lands next boundary.
  │     ContextEviction wraps outside MessagePersistence: the raw result is
  │     persisted first, then replaced by an evicted-head/tail preview before it
  │     reaches state; read_file results are sliced, never offloaded. Its
  │     before_model tags an oversized trailing HumanMessage (full text stays in
  │     state/MesMemory) and wrap_model_call truncates only the model view)
  │    ThinkingControl: per-session thinking toggle (client switch →
  │     PUT /sessions/thinking → state register); swaps request.model for the
  │     thinking on/off variant built by build_main_llm(thinking=...) when the
  │     session carries an explicit LLM_THINKING_ENABLED flag, else env default
  │
  ├─ tools: build_main_tools() → taskflow(14) + todolist(2) + memory + subagent(7)
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

  completion gates (all always-on, all fail-open):
    · StepJudge — step_judge.py, on taskflow_resume for criteria-bearing steps
      (pass / retry within STEP_JUDGE["max_retries"] / block)
    · CompletionJudge goal loop — every spawn, bounded by
      COMPLETION_JUDGE["goal_max_turns"]; continue injects a follow-up turn
    · Evidence ledger — always-on terminal/python_repl auto-record + file-edit
      stale events; read side derives staleness (evidence_collector.py)
    · taskflow_finish gates A–D — DAG completeness, no blocked steps, no
      FAIL/[stale] evidence, SisyphusVerifier (only with todo + plan_path)
    · SubagentCompletionDrainMiddleware — unconditional programmatic gate on
      drained completion carriers when the session lacks passing evidence
```

## Concurrency Lanes (`runtime/lane/`)

Four process-level lanes, each an `asyncio.Semaphore` + active/queued counters, gate concurrent work instead of rejecting it: over-limit work waits FIFO.

| Lane | Constrains | Default | Config key |
|---|---|---|---|
| `MAIN` | main-agent turn (`_run_executor`) | `min(16, max(8, CPU))`, clamped up to `SUBAGENT + NUDGE` (12) → 12–16 | `LANE_SYSTEM["main_max_concurrent"]` |
| `SUBAGENT` | child-agent executions (spawn + steer) | 8 | `LANE_SYSTEM["subagent_max_concurrent"]` |
| `NUDGE` | nudge/persistence calls (`summarization/nudges.py`, 3 sites) | 4 | `LANE_SYSTEM["nudge_max_concurrent"]` |
| `NESTED` | `sessions_send` reply turns (serial) | 1 | `LANE_SYSTEM["nested_max_concurrent"]` |

- Config: `config/features/infra_side/lane_system.py`; `validate_lane_config()` runs at server startup and enforces `main >= subagent + nudge` (all limits ≥ 1); `install_lane_lifecycle()` in `server/service/lane_lifecycle.py` validates, prewarms the manager, registers `set_drain_check(is_gateway_draining)`, and installs a bounded exit drain (`atexit`, `drain_all(timeout=0)` — never blocks exit).
- Queueing: a spawn that passes per-parent admission but exceeds the SUBAGENT lane is registered `PENDING` (no `forbidden`); `PENDING → RUNNING` happens inside the lane slot and only then is `started_at` stamped (queue wait is not run time). `PENDING` counts as active in registry queries and is killable; restore finalizes restart-leftover PENDING runs the same way at startup (silently — no announce), and orphan recovery covers same-lifetime lost tasks with `ended_reason="pending_orphaned"`.
- Drain mode: `set_drain_check(fn)` makes `Lane.acquire()` refuse (without consuming a permit) while the subagent gateway reports draining.
- Observability: `GET /lane-status` → `{main|subagent|nudge|nested: {name, max_concurrent, active, queued}}`.
- Hot updates (`LaneManager.set_concurrency`) only affect new acquires; in-flight slots keep their permits.

## Key Configuration

| File | Contents |
|---|---|
| `config/features/agent_side/` | 32 per-object TypedDicts (summarization, guardrails, tool_result_eviction, iteration, memory_flush, taskflow_infra, todolist_infra, tools_timeouts, step_judge, completion_judge, evidence_ledger, ...) |
| `config/features/infra_side/` | 19 per-object TypedDicts (gateway, bus, http_upload, retry_backoff, server_http, ws_stream, input_queue, heartbeat, cron, skill_scanner, mes_memory, curator, model_pricing, ...) |
| `config/features/__init__.py` | Aggregator — all 51 TypedDicts + instances re-exported |
| `config/path.py` | All filesystem paths (ROOT_DIR, SKILLS_DIR, WORKSPACE_DIR, ...) |
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
  - `skills/builtin/**` user scripts may still import `models`/`bus`/`channels`/`workspace`/`runtime` (one-way, by design) — this ban covers `server/**` only; whether to tighten those directions is evaluated separately and is not currently enforced
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

`tests/full/` is excluded from the split runner (`--ignore`) and every live-LLM file there is tagged `llm_e2e`, so a bare `pytest` run never collects them. Run one explicitly with `uv run --no-sync pytest -m llm_e2e tests/full/<file>` — addopts default to `not llm_e2e`, so the explicit `-m` is required. Because process startup/teardown dominates the wall clock, run `tests/full/` **one file per process under an external watchdog** (e.g. `timeout 1500 uv run --no-sync pytest -m llm_e2e tests/full/<file>`); measured: 47 cases ≈ 10.8 min of net test time but ≈ 73.7 min wall clock when batched. Hermetic tests belong in the standard tree, not `tests/full/`.

**IMPORTANT**: `tests/agent/tools/subagent/conftest.py` installs sys.modules stubs at collection time. When running multiple test dirs in one process, use the stub-tolerant loading pattern from `tests/agent/tools/taskflow/conftest.py`. The split runner (`tests/run_tests_split.py`) avoids this by running groups in separate processes.

## Conventions

- **Python 3.13** (StrEnum, PEP 695 type aliases, typing.override)
- **Config**: one TypedDict + one instance per feature in `config/features/<side>/<name>.py`, re-exported via `__init__.py` — NEVER use bare dict literals for feature config
- **Middleware hooks**: `abefore_model(self, state, runtime=None)`, `aafter_agent(self, state, runtime=None)` — LangChain 1.3.9 signatures; after_agent runs in REVERSE list order
- **Tool registration**: `@tool("name")` + `build_*_tools()` factory + `_MAIN_TOOLS_BUILDERS` list in `agent/tools/__init__.py`
- **Test fixtures**: `isolated_db` (tmp SQLite), `build_main_tools_real` (stub-tolerant), `scan_skills_real`
- **Docs parity** (`scripts/check_docs_parity.py`): gates every four-language README group on structure plus semantic metrics — link-target sets with per-target language variants (a translation that links a foreign README the EN reference does not link fails, while the language switcher and own-language links pass), per-section body-length ratio plus a non-empty check for even the smallest EN sections, a language-independent heading-marker sequence (inline code spans + link targets, order-canonicalized) that fails a heading reordered under an unchanged level sequence, and a per-section invariant-token multiset (code spans + file paths + ENV names + numbers, matched by substring so dropped backticks are not drift) that fails a long but stale translation which lost ≥2 code-span/path tokens or all of them. Section pairing matches headings by marker fingerprint first, then occurrence index (the index-fallback count and the marker-less-reorder residual limitation are printed in the summary). The ratio band is a calibration snapshot: `--calibrate` prints the per-language distribution and its margin, and `tests/scripts/test_check_docs_parity.py` pins the smallest legitimate ratio at ≥1.15× the band so a denser legitimate section turns the test red before the band silently narrows — re-run `--calibrate` and update the constants' comment before changing any band. Exempting a group via `ALLOWLIST` is a last resort: the reason must contain an explicit "Fixing the document(s) is wrong because …" clause followed by a substantive rationale, be ≥40 chars, and may not open with placeholder text; stale entries fail the gate, and tests pin the mapping empty so every addition requires an explicit test edit
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
- `asyncio.Semaphore` is event-loop-bound, so lanes must be acquired on the main loop — a cross-loop `acquire()` logs a warning and rebinds a fresh semaphore with outstanding slots deducted (never double-issues permits)
- `.gitignore` line `*.db` ignores all SQLite files — DB files are never committed
- pre-push hook runs basedpyright on the entire diff — must be 0 errors before push
