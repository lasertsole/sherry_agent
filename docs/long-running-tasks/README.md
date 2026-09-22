# ⏳ Long-Running Tasks: TaskFlow, Budgets, Deadlines, Memory & Continuity

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> How the agent runs work that outlives a single turn: a durable SQLite DAG engine (`taskflow_*`, 14 tools) tracks dependent steps across conversation turns, dispatches each step to a detached child subagent, retries failed or dead steps per an opt-in policy, judges criteria-bearing step results with an auxiliary LLM (`pass` / `retry` / `block`), aggregates token/cost spend against a budget, expires overdue or idle flows from a background sweeper, exposes a session-scoped flow board (every read filters the owning session in SQL; subagents never receive taskflow/todolist/knowledge tools), and carries context forward through a two-layer memory system, a pre-compression memory flush, a summary↔TaskFlow bridge, one-line tool-output summaries, cross-session continuity, subagent-completion memory backflow, and automatic re-injection of active flows into the system prompt.

Source of truth: `agent/tools/taskflow/**`, `agent/tools/memory.py`, `agent/middlewares/summarization/memory_flush.py`, `agent/middlewares/summarization/core.py` (TaskFlow-context block), `agent/middlewares/subagent_completion_drain/core.py` (memory backflow), `agent/middlewares/task_intent/core.py`, `agent/middlewares/todo_continuation/core.py`, `context_engine/session_continuity.py`, `workspace/prompt_builder.py`, `pub/func/message/tool_output_prune.py`, `agent/tools/subagent/registry/sweeper.py`, `agent/wrapper/**`, `config/features/**`. Every constant, signature and line number below was verified against that code.

## Table of Contents

- [Overview](#-overview)
- [TaskFlow Engine](engine/README.md)
  - [TaskFlow DAG Engine](engine/README.md#-taskflow-dag-engine)
  - [Step Retry Policy](engine/README.md#-step-retry-policy)
  - [Token / Cost Budget](engine/README.md#-token--cost-budget)
  - [Task Deadline](engine/README.md#-task-deadline)
  - [Result Validation](engine/README.md#-result-validation)
  - [Progress Report](engine/README.md#-progress-report)
  - [Idle Detection](engine/README.md#-idle-detection)
  - [Session-Scoped Board & Isolation](engine/README.md#-session-scoped-board--isolation)
- [Memory & Continuity](memory/README.md)
  - [Layered Memory](memory/README.md#-layered-memory)
  - [Pre-Compression Memory Flush](memory/README.md#-pre-compression-memory-flush)
  - [Summary ↔ TaskFlow Coordination](memory/README.md#-summary--taskflow-coordination)
  - [Subagent Memory Backflow](memory/README.md#-subagent-memory-backflow)
  - [Tool Output Summarization](memory/README.md#-tool-output-summarization)
  - [Session Continuity](memory/README.md#-session-continuity)
  - [TaskFlow Auto-Resume](memory/README.md#-taskflow-auto-resume)
- [Concurrency Lanes](lanes/README.md)
- [Configuration Registry](#-configuration-registry)
- [Architecture Diagram](#-architecture-diagram)
- [API Reference](#-api-reference)
- [Testing](#-testing)
- [Known Limitations](#-known-limitations)

## 🎯 Overview

The long-running-task stack lets the main agent decompose a multi-turn job into a **durable flow** whose steps may depend on each other, dispatch each ready step to a child subagent, and survive process restarts. Six subsystems cooperate:

| # | Subsystem | Entry point | Durable where |
| :- | :--- | :--- | :--- |
| 1 | **TaskFlow DAG engine** | `agent/tools/taskflow/` | `data/taskflow_registry.db` (SQLite, WAL) |
| 2 | **Token / cost budget** | `taskflow_budget`, `taskflow_resume` | `task_flows.total_tokens` / `total_cost` / `token_budget` |
| 3 | **Deadline** | `taskflow_create(deadline_hours=…)` + sweeper | `task_flows.deadline_ts` |
| 4 | **Idle detection** | sweeper `_scan_stale_waiting_taskflows` | `wait_json` stale markers |
| 5 | **Pre-compression flush** | `agent/middlewares/summarization/memory_flush.py` | `workspace/memory/MEMORY.md` |
| 6 | **Continuity / auto-resume** | `context_engine/session_continuity.py`, `workspace/prompt_builder.py` | `src/data/session_continuity/*.json` + prompt blocks |

The design contract throughout is **error-as-text**: tools never raise business errors at the model; they return human-readable strings beginning with `Error:`. Every background hook is **fail-open** — an unavailable registry or crashed sweeper degrades to "no long-running-task context", never to a broken turn.

## ⚙️ Configuration Registry

All tunables live under `config/features/`, which is a **per-object `TypedDict` package** — not a single monolithic module. It is split into three parts:

| Part | Contents |
| :--- | :--- |
| `config/features/agent_side/` | **26** agent-side config modules (middlewares, tools, LLM client, memory, TaskFlow) |
| `config/features/infra_side/` | **19** infra-side config modules (server, queues, skills, context engine, runtime, model pricing) |
| `config/features/_env.py` | The single shared env helper |

Each module defines `class XxxConfig(TypedDict)` plus a module-level constant `XXX: XxxConfig = {…}`. Env-aware modules define a builder `def _build_xxx(env: Mapping[str, str] | None = None) -> XxxConfig` that reads `env or os.environ` and materialises the constant at import time. The env helper is `_env_int(name, default, env)` (`config/features/_env.py:9`), which accepts `1/true/yes/on` and `0/false/no/off/""` and never raises.

The registry currently holds **45 feature objects** — 26 agent-side + 19 infra-side — re-exported through each package `__init__.py` and aggregated by `config/features/__init__.py`, so a consumer imports either one half or the whole registry from a single place. Consuming code imports the constant and indexes it directly (for example `ITERATION_BUDGET["default_max_iterations"]`); there is no `get_feature`/`load_feature` accessor. `config/__init__.py:38-39` derives `API_HOST`/`API_PORT` from `GATEWAY`.

The constants most relevant to this document:

| Config object | Field | Value |
| :--- | :--- | :--- |
| `TASKFLOW_INFRA` (`agent_side/taskflow_infra.py`) | `busy_timeout_ms` | 5000 |
| | `init_wait_timeout_s` | 10.0 |
| | `persist_max_attempts` | 3 |
| | `wait_all_min_poll_interval_seconds` | 0.05 |
| | `wait_all_default_timeout_seconds` | 300.0 |
| | `wait_all_default_poll_interval_seconds` | 0.5 |
| | `waiting_timeout_hours` | 24 |
| `MODEL_PRICING` (`infra_side/model_pricing.py`) | `model_pricing_per_m_tokens` | `glm-5` / `deepseek-chat` / `kimi-latest` / `_default` |
| | `budget_warn_threshold` | 0.80 |
| `MEMORY_FLUSH` (`agent_side/memory_flush.py`) | `enabled` | `MEMORY_FLUSH_ENABLED` (default 1) |
| | `model` | `MEMORY_FLUSH_MODEL` (default "") |
| | `soft_threshold_tokens` | 8000 |
| | `force_flush_chars` | 50000 |
| | `output_max_tokens` | 2048 |
| | `timeout_seconds` | 30 |
| `SUMMARIZATION` (`agent_side/summarization.py`) | `prune_protect_tokens` | 40000 |
| | `prune_min_reduction_tokens` | 5000 |
| | `protected_tools` | `{"memory", "skill_view", "skill_list"}` |
| `MES_MEMORY` (`infra_side/mes_memory.py`) | `busy_timeout_s` / `connect_attempts` | 10.0 / 5 |

## 🏗️ Architecture Diagram

```
                    ┌────────────────────────────────────────────────────────┐
                    │              agent/wrapper/ registry                   │
                    │  apply_graph_wrappers() → innermost-first chain:       │
                    │  RepetitionGuardWrapper → ContextLimitGuardWrapper     │
                    └───────────────────────────┬────────────────────────────┘
                                                │ wraps the compiled graph
                          ┌─────────────────────▼────────────────────────┐
                          │                MAIN AGENT                     │
                          │  create_agent + middleware chain             │
                          └───────────────┬──────────────────────────────┘
                                          │
        ┌─────────────────────────────────┼─────────────────────────────────────────┐
        ▼                                 ▼                                         ▼
┌───────────────────┐          ┌──────────────────────┐                 ┌────────────────────────┐
│ taskflow_* tools  │          │  memory tool         │                 │ prompt_builder         │
│ (14, main_only)   │          │  add/replace/remove  │                 │ build_system_prompt    │
└────────┬──────────┘          └──────────┬───────────┘                 └───────────┬────────────┘
         │                                │                                         │
         ▼                                ▼                                         ▼
┌───────────────────┐          ┌──────────────────────┐                 ┌────────────────────────┐
│ TaskFlow store    │          │ MemoryStore (L1)     │                 │ ─ MEMORY/USER snapshot │
│ task_flows (WAL)  │          │  agent/tools/        │                 │ ─ Pending TaskFlows    │
│ state_json DAG    │          │  memory.py           │                 │ ─ Last Session         │
│ + retry/validation│          │                      │                 │                        │
└────────┬──────────┘          └──────────────────────┘                 └────────────────────────┘
         │ dispatch_child()                                                       ▲
         ▼                                                                        │ continuity json
┌───────────────────┐     announce/settle     ┌────────────────────────┐           │
│ child subagent    │ ──────────────────────▶ │ taskflow_resume        │           │
│ sessions          │                         │ (+ token_usage budget) │           │
└───────────────────┘                         └───────────┬────────────┘           │
         │                                                │                        │
         │ drain                                          │                        │
         ▼                                                │                        │
┌──────────────────────────────┐                          │                        │
│ SubagentCompletionDrain      │                          │                        │
│ _backflow_shared_memory      │                          │                        │
└──────────────────────────────┘                          │                        │
         ┌────────────────────────────────────────────────┘                        │
         ▼                                                                         │
┌──────────────────────────────┐    every sweep    ┌───────────────────────────┐   │
│ SUBAGENT SWEEPER             │◀─────────────────▶│ Summarization middleware  │   │
│ _expire_overdue_taskflows    │                   │ prune → memory_flush →    │   │
│ _scan_stale_waiting_taskflows│                   │ summary (+TaskFlow)       │   │
└──────────────────────────────┘                   │                           │   │
                                                   └───────────┬───────────────┘   │
                                                               │ clear_session      │
                                                               ▼                    │
                                                   ┌───────────────────────────┐    │
                                                   │ session_continuity JSON   │────┘
                                                   └───────────────────────────┘
```

The compiled graph is wrapped by the **`agent/wrapper/`** package, which owns the guards. `agent.wrapper.registry` exposes a process-global, ordered, pluggable chain (`register_graph_wrapper`, `unregister_graph_wrapper`, `apply_graph_wrappers`, `reset_graph_wrappers`) with `GraphWrapperFactory` entries applied **innermost-first**; the default entries are `RepetitionGuardWrapper(phantom_stream_guard=True)` then `ContextLimitGuardWrapper(context_window=main_llm_max_tokens)`. The stream repetition guard lives in `agent/wrapper/repetition_guard.py` and the context-window guard in `agent/wrapper/context_limit.py`. The **memory backflow** is performed by `SubagentCompletionDrainMiddleware` in `agent/middlewares/subagent_completion_drain/core.py`.

## 📚 API Reference

### TaskFlow tools

| Tool | Signature | Returns |
| :--- | :--- | :--- |
| `taskflow_create` | `(flow_id, description="", initial_state=None, session_id, deadline_hours=None)` | created id/status/revision (+ deadline) |
| `taskflow_run_task` | `(flow_id, task, label=None, expected_revision=None, depends_on=None, validation_criteria=None, retry_policy=None, session_id)` | dispatched step, or `blocked` with pending deps |
| `taskflow_dispatch` | `(flow_id, step_ids, expected_revision=None, session_id)` | dispatched step ids + revision |
| `taskflow_wait_all` | `(flow_id, timeout_seconds=300.0, poll_interval_seconds=0.5, session_id)` | per-step settled report (complete or partial; auto-retries policy steps) |
| `taskflow_resume` | `(flow_id, child_session_key="", result="", expected_revision=None, token_usage=None, validation_criteria=None, session_id)` | resumed status, unlocked steps, step-status counts, criteria echo, judge verdict, retry note |
| `taskflow_set_waiting` | `(flow_id, wait_reason="", expected_revision=None, session_id)` | waiting status + revision |
| `taskflow_summary` | `(flow_id, session_id)` | full flow state incl. wait/deadline status |
| `taskflow_progress` | `(flow_id, session_id)` | completion %, breakdown, next steps, est. remaining |
| `taskflow_budget` | `(flow_id, action="query", token_budget=None, expected_revision=None, session_id)` | budget report, or set confirmation |
| `taskflow_list` | `(status_filter="active", session_id)` | this session's board (`active` / `all` / status name) |
| `taskflow_finish` | `(flow_id, summary="", expected_revision=None, todo=None, plan_path=None, checkbox_label=None, session_id)` | terminal `done` (four-gate check, fail-open) |
| `taskflow_fail` | `(flow_id, reason="", expected_revision=None, session_id)` | terminal `failed` |
| `taskflow_cancel` | `(flow_id, reason="", expected_revision=None, session_id)` | terminal `cancelled` |

### Memory tool actions

| Action | Signature | Returns |
| :--- | :--- | :--- |
| `add` / `replace` / `remove` | `memory(action, target="memory"\|"user", content, old_text)` | JSON success/error |

### Key functions & constants

| Symbol | Location | Role |
| :--- | :--- | :--- |
| `TaskFlowStatus` / `StepStatus` | `agent/tools/taskflow/config.py:11,21` | Lifecycle / DAG enums |
| `update_flow` | `agent/tools/taskflow/registry/store_sqlite.py` | Optimistic-locked session-scoped mutation |
| `get_active_flows_sync` | `agent/tools/taskflow/registry/store_sqlite.py` | Session-scoped active-flow read (SQL `session_id` filter) |
| `get_overdue_flows` / `get_waiting_flows` | `store_sqlite.py` | Sweeper queries (deliberately cross-session) |
| `deps_satisfied` / `unlock_dependents` | `agent/tools/taskflow/tools/_shared.py:115,153` | DAG transitions |
| `update_flow_with_conflict_retry` | `_shared.py:191` | Never-lose-a-spawned-child persist |
| `_expire_overdue_taskflows` | `agent/tools/subagent/registry/sweeper.py:123` | Deadline enforcement |
| `_scan_stale_waiting_taskflows` | `sweeper.py:154` | Idle detection marker |
| `_get_taskflow_context_sync` | `agent/middlewares/summarization/core.py:122` | summary coordination |
| `_build_taskflow_block` | `workspace/prompt_builder.py:145` | Auto-resume prompt block |
| `prune_tool_outputs` | `pub/func/message/tool_output_prune.py:104` | One-line tool summaries |
| `auto_save_on_session_end` | `context_engine/session_continuity.py:117` | Continuity save hook |
| `should_flush` / `run_memory_flush` | `agent/middlewares/summarization/memory_flush.py:43,65` | Pre-compression flush |
| `append_entries` | `agent/tools/memory.py:281` | Batch MEMORY.md append |
| `get_all_flows_sync` | `agent/tools/taskflow/registry/store_sqlite.py` | Session board read (SQL `session_id` filter) |
| `classify_failure` / `should_retry_failure` | `agent/tools/taskflow/tools/_retry.py:56,103` | failure classification |
| `plan_settled_retries` / `persist_retry_actions` | `agent/tools/taskflow/tools/_retry.py:218,273` | wait_all retry planning/persist |
| `_backflow_shared_memory` | `agent/middlewares/subagent_completion_drain/core.py:93` | memory backflow reconcile |
| `judge_step_result` | `agent/tools/taskflow/step_judge.py:142` | step-level pass/retry/block verdict |
| `collect_evidence_summary` | `agent/tools/taskflow/evidence_collector.py:22` | judge-prompt evidence summary |
| `apply_graph_wrappers` | `agent/wrapper/registry.py:69` | Pluggable graph-wrapper chain |

### Synthesize aggregation (`aggregate_deps`)

`taskflow_run_task(aggregate_deps=true)` marks a synthesis step whose dependency
results are appended to the dispatched task as a `## Upstream Results` block.
`build_task_with_dep_results(step, steps, results)` matches each dependency's
`child_session_key` against the flow's `{child_session_key, result, result_hash}`
records (in `depends_on` order) and falls back to a `no result recorded`
placeholder when a result is missing. The aggregation is re-derived at every
re-dispatch from the stable results, so the stored step task is never rewritten;
omitting the flag keeps the legacy dispatch text unchanged.

## 🧪 Testing

The TaskFlow suite lives under `tests/agent/tools/taskflow/` (twenty-four `unit` test files plus a shared `conftest.py`):

| Test file | Covers |
| :--- | :--- |
| `test_store_sqlite.py` | CRUD, revision bumping, optimistic-concurrency conflict, WAL, sync accessors, active/waiting/terminal filters, session isolation (`session_id` scoping, cross-session update refusal), additive `session_id` migration, session purge |
| `test_step_graph.py` | `deps_satisfied`, `mark_step_done`, `unlock_dependents`, legacy status derivation, self-dependency guard |
| `test_summary_dag.py` | `taskflow_summary` DAG rendering |
| `test_resume_dag.py` | Resume marks done, unlocks dependents, partial completion, idempotent no-op |
| `test_taskflow_tools.py` | Full create→run→resume→finish across restart, conflict, terminal transitions |
| `test_taskflow_dispatch.py` | Batch dispatch, all-or-nothing validation, mid-batch failure persistence |
| `test_dag_e2e.py` | Full parallel DAG flow across a process restart |
| `test_conflict_persistence.py` | Spawned-children persistence after conflict, retry exhaustion |
| `test_taskflow_wait_all.py` | Flow-scoped wait, timeout partial report, unrelated live child |
| `test_run_task_dag.py` | Blocked registration, satisfied dispatch, unknown-dependency error |
| `test_taskflow_progress.py` | Completion/breakdown/next-steps/est-remaining/waiting |
| `test_token_budget.py` | Token aggregation, cost math, budget query/set/warning/exceeded |
| `test_deadline.py` | `deadline_hours`, summary rendering, sweeper expiry |
| `test_idle_detection.py` | Active/stale wait status, sweeper marker, live-child skip |
| `test_retry_policy.py` | policy validation, failure classification, re-dispatch, exhaustion |
| `test_validation.py` | criteria storage, judge verdicts (`pass`/`retry`/`block`), override, fail-open |
| `test_step_judge.py` | StepJudge response parsing, verdict coercion, fail-open to `pass`, prompt assembly |
| `test_resume_with_judge.py` | `taskflow_resume` + judge: pass/retry/block, retry-feedback injection, budget exhaustion, fail-open |
| `test_evidence_collector.py` | evidence summary rendering for judge prompts; staleness derived from later `stale` rows |
| `test_finish_gate.py` | finish gates A–D: DAG completeness, blocked steps, failing/stale evidence, `SisyphusVerifier` |
| `test_chain_smoke.py` | integrated judge → retry-feedback → goal-loop → finish-gate smoke against a stub auxiliary LLM |
| `test_taskflow_list.py` | session board rendering, status filters, last-activity timestamp |
| `test_index_audit.py` | SQLite index upgrade path and query-plan audit |
| `test_update_steps.py` | full steps replacement: add/remove/rewrite/reorder and the dispatched/done safety rules |

Cross-cutting suites: `tests/agent/middlewares/test_memory_flush.py` (flush thresholds and `append_entries`), `tests/agent/middlewares/test_lt5_memory_backflow.py` (memory reconcile on completion drain), `tests/agent/middlewares/test_subagent_completion_drain_reminder.py` (completion-carrier verification reminder), `tests/agent/middlewares/test_completion_drain_gate.py` (unconditional programmatic gate), `tests/agent/tools/subagent/test_completion_judge.py` + `test_goal_loop.py` (completion judge and the bounded goal loop), `tests/agent/tools/test_evidence_auto_record.py` + `test_evidence_stale.py` + `tests/agent/tools/todolist/test_evidence_ledger.py` (evidence recording, stale events, ledger views), `tests/context_engine/test_session_continuity.py` (continuity save/prompt), `tests/agent/middlewares/test_todo_continuation.py` (turn-end continuation), `tests/pub/func/message/test_tool_output_prune.py` (one-line summaries), and `tests/workspace/test_prompt_builder_taskflow.py` (pending-flow prompt injection).

Run just this area with the standard uv/pytest tooling:

```bash
uv run pytest tests/agent/tools/taskflow -q
uv run pytest tests/agent/middlewares/test_memory_flush.py tests/context_engine/test_session_continuity.py -q
uv run pytest tests/pub/func/message/test_tool_output_prune.py -q
```

For the full process-isolated suite use `uv run python tests/run_tests_split.py` (Group A runs the `unit` files, Group B runs the `module`/`integration` files).

## ⚠️ Known Limitations

- **`taskflow_list` is session-scoped.** Every read filters the owning `session_id` in SQL, so a session can no longer enumerate another session's flows; there is no global board. Pre-isolation rows (`session_id = ''`) are invisible to session reads but still resolved by the sweeper's cross-session deadline/idle scans.
- **`done` is not success.** A step's `done` means "a result was injected"; there are no `failed`/`skipped` step statuses. `taskflow_resume` marks the step `done` and unlocks successors even when the child reported an error. Failure-aware step transitions are intentionally deferred.
- **`taskflow_wait_all` is flow-scoped by design.** It waits only on children recorded on the given flow's dispatched steps, and unknown/already-cleaned runs count as settled. There is no global "wait for every active flow" primitive.
- **Idle detection is advisory.** The sweeper stamps `stale_detected_at` / `stale_child_session_key` into `wait_json` but never auto-fails a stale `waiting` flow; a human or the model must act on the marker.
- **The pre-compression memory flush is latent.** Production instantiations of `Summarization` (main agent and subagent) do not pass `memory_store` / `llm_factory`, so the flush does not run until a call site wires them; the code is implemented and tested but currently inert.
- **Continuity is channel-bound.** `build_continuity_prompt` requires both a channel id and a chat id, so sessions without a channel binding receive no continuity block. Storage is per-key JSON on disk, not a database.
- **Three duplicate active-flow scans.** `prompt_builder._build_taskflow_block`, `summarization._get_taskflow_context_sync`, and `session_continuity._get_active_taskflow_ids_sync` implement the same query independently; they must be kept in sync.
- **Registry size is 45.** The config registry holds 45 feature objects (26 agent-side + 19 infra-side); the infra-side contract test covers 18 of them (GATEWAY plus 17 data-driven cases) and omits `MODEL_PRICING`.
- **Package re-export gap.** `agent/tools/taskflow/__init__.py` re-exports only eleven names; `taskflow_dispatch` and `taskflow_wait_all` are reachable through `build_taskflow_tools()` but omitted from the package `__all__`.
- **The TaskFlow block is LLM-prompt only.** The deterministic fallback summary used on LLM failure does not include `## Current TaskFlow State`.
- **Token accounting is caller-supplied.** Cost is computed only when `taskflow_resume` receives a `token_usage` dict; steps whose results are injected without it contribute zero tokens and zero cost.
- **Result validation is judge-enforced when criteria exist.** A criteria-bearing step is reviewed by the auxiliary-LLM step judge on resume: `retry` re-dispatches it within the step's own retry budget, and `block` (or an exhausted budget) marks it `blocked`. The judge is fail-open, so a model error or an unparseable response degrades to `pass` — there is still no hard guarantee that a result met its criteria, only a best-effort verdict before the step is accepted.
- **Retry classification is text-based.** `classify_failure` is a substring heuristic over the result text: a failure phrased outside the pattern table (or a genuine failure hidden by a negated phrase) will not trigger a retry, while an empty `retry_on` retries every classified failure. `taskflow_wait_all` cannot classify a dead child with no result text, so it always consumes retry budget while one remains.
- **`taskflow_list` is session-scoped.** There is no global cross-session board: every read filters the owning `session_id` in SQL, so a session cannot enumerate another session's flows. Pre-isolation rows (`session_id = ''`) are invisible to session reads but still resolved by the sweeper's cross-session deadline/idle scans.
- **Knowledge storage is keyed by plan identity, not by plan name.** Access is gated by ownership (`ownership.is_plan_associated()`: the session's `plan_ref`, a todo `plan_ref`, or a boulder work whose `session_ids` include the session), and the storage directory derives from the canonical plan path — `sha1(repo-relative path)[:12]` under `workspace/knowledge/plans/<plan_key>/`, with a `meta.json` recording the readable `plan_name` / `plan_ref`. Two sessions that hold same-named plans **in different plan files are physically isolated** (each writes its own key directory); sessions collaborating on **one plan file** through boulder `session_ids` resolve the same path and share one directory. A session whose plan file does not resolve writes under the fallback identity `session-<sha1(session_id)[:8]>` (the full-id hash keeps ids sharing an 8-char prefix apart). Legacy name-keyed directories remain readable; writes always land in the key directory, and `clear_session` deletes the session's private identity directories while retaining plans shared with another session. The subagent boundary stays absolute — `knowledge` is `main_only`.
