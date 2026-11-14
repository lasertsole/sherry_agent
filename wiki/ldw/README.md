# 🔁 LDW Replay

**English** · [中文](README.zh.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

> Workflow-level replay for verified TaskFlow plans. The step DAG is compiled into structured metadata, and the vendored [langchain-dynamic-workflow](https://github.com/OWNER/langchain-dynamic-workflow) core executes it with a content-hash journal. A failed LDW replay automatically falls back to a NEW TaskFlow flow built from the same verified DAG, then writes the successful results back to the journal.

Source of truth: `vendor/ldw/**`, `agent/tools/ldw/**`, `agent/tools/subagent/ldw_shim.py`, `config/features/agent_side/ldw.py`, `tests/agent/tools/ldw/**`, `tests/full/test_ldw_real_plan_e2e.py`, and `tests/agent/tools/subagent/test_ldw_shim.py`.

## 🎯 Overview

LDW replay is a **second execution path** for a plan that already succeeded through TaskFlow:

| Path | Granularity | Cache | Trigger |
|---|---|---|---|
| TaskFlow | step / subagent | evidence ledger + registry | normal plan execution |
| PTC replay | tool calls inside a step | skill script | executor spawn prefill |
| LDW replay | step DAG | journal keyed by prompt + agent_type + model + schema | `workflow_replay` tool after the replay threshold |

The LDW core is vendored under `vendor/ldw/`, with a minimal `deepagents` stub installed at import time. Sherry supplies its own LangGraph leaf runnables, so the LDW sandbox/leaf-construction modules are never called.

## 🧭 Trigger and Tool

The main agent has a `workflow_replay` tool (main-only):

```json
{"action": "status", "variant_input": "", "flow_id": "", "plan_key": "", "force": false}
```

- `status` reports the replay count, the minimum threshold, the compiled script path, and whether the session currently has a readable TaskFlow DAG.
- `compile` builds `ldw__<plan_key>.py` from the current TaskFlow flow DAG.
- `replay` increments the replay count, then either stays on TaskFlow (below `LDW["min_replays"]`) or runs LDW; a partial LDW run automatically enters the TaskFlow fallback.
- The returned JSON is structured: `status=ok|taskflow|degraded|partial|no_workflow|no_plan|disabled|error`, plus `failed_steps`, `journal_hits`, `leaf_calls`, `degraded`, and repair fields where applicable. `ok` is true for `ok`, `taskflow`, and `degraded`.

The replay threshold is deliberately lazy: low-frequency plans never pay the LDW setup cost. A plan is compiled at the threshold (default 3) from its current TaskFlow flow, not eagerly by skill review.

`variant_input` is the current task's concrete input. It is injected into every step prompt and therefore enters the journal key; the same plan with a different variant does not hit the old cache. The step suffix is also part of `agent_type`, because PTC scripts are step-scoped.

## 🧱 Compilation and Artifacts

`compile_plan_to_workflow(plan_knowledge)` returns JSON-persistable metadata:

```json
{"plan_key": "...", "flow_id": "...", "steps": [{"step_id": "step-1", "task_template": "...", "agent_type": "executor@step-1", "deps": [], "response_schema": null, "step_timeout_seconds": null, "retry_policy": null}]}
```

`build_orchestrate(meta, variant_input)` builds the LDW `orchestrate` callable. Each `DagNode` receives its predecessors' results, and the prompt carries `[Current task input]`, `[Hard constraints]`, plus `[Previous step results]`; this is what keeps the content-hash journal correct across variants and upstream outputs. A schema-bound step calls `ctx.agent(..., schema=...)`, so the leaf emits a validated structured object rather than free text.

Artifacts live at the top level of an owning skill's `scripts/`:

```text
skills/auto/<owning-skill>/scripts/ldw__<plan_key>.py
skills/auto/<owning-skill>/scripts/ptc__<plan_key>__<step_id>.py
```

The LDW script stores `PLAN_KEY`, `REQUIRED_AGENT_TYPES`, and `WORKFLOW_META` as Python module constants. It does not store closures, source strings, or `workflow.json` sidecars. The writer uses Python literals, not JSON text, because `json.dumps` emits lowercase `true` / `false` / `null` and a schema containing `additionalProperties: false` otherwise makes the script unloadable.

## ⚙️ Runtime and Journal

- `register_roster(meta, session_id)` registers one builder PER STEP. The builder lets LDW request a schema-bound runnable, and the step suffix in `agent_type` keeps the journal and PTC lookup step-scoped.
- Leaves reuse the real child contract: `_assemble_child_tools` applies the role allow-list and injects code intelligence, ast-grep, and executor-only PTC. The session id travels through the runnable config; it is never baked into a shared graph.
- The leaf's model follows the role tier and `step_model` override. A schema-bound leaf forces thinking off, matching the child agent's structured-output last resort.
- On a journal miss an executor leaf may run the step's PTC script through `run_ptc` first. PTC failure appends `[PTC Pre-run Progress]` and continues with ReAct; a PTC schema miss is a failure, never success.
- `SqliteWorkflowStore` at `src/data/workflow_runs.db` backs cross-process resume. A repeat replay with the same prompt / agent_type / model / schema returns the journaled result with zero leaf calls. The per-run result reports `journal_hits` and `leaf_calls`.
- Per-plan locks serialize concurrent replay. `LDW["real_replay_timeout_s"]` bounds one LDW run; a timeout is a failed run and therefore a fallback candidate, not a hang.

## 🪜 Automatic TaskFlow Fallback

When an LDW run fails and `LDW["auto_degrade_enabled"]` is on, the adapter rebuilds execution from the SAME verified DAG. It never resets or re-runs the old flow.

1. Create a new `ldw-<first 8 chars of plan_key>-<8-char uuid>` TaskFlow flow with `resumed_from=<origin>` when the origin still exists.
2. Seed every non-None LDW result as a DONE step with a synthetic `ldw-journal:<flow>:<step>` child key and result record; only failed or unfinished steps stay ready/blocked.
3. Dispatch ready steps through normal TaskFlow, so retry policies, the schema gate, the StepJudge, and child completion all behave exactly as in normal execution.
4. Poll with `taskflow_wait_all`, then inject each settled child with `taskflow_resume`. A timed-out or killed child is declared a failure; `auto_dispatch` starts the next wave.
5. Finish a fully done flow. A rejected finish or unfinished work is terminalized (fail or cancel) instead of leaving an active orphan flow.
6. Write successful TaskFlow results back to the LDW journal using the same prompt/agent_type/model/schema keys, so the next LDW replay of that variant is zero-call.

When steps failed, `repair_extraction` reads the replay flow's child handoffs, merges the actual success path and failure branches into plan knowledge, persists `success-steps.json`, and recompiles the LDW script. Repair is fail-open: a persistence or recompile error is returned in the result but never turns a replayed result into a crash. PTC scripts are owned by the skill-review compiler; LDW repair only recompiles its own artifact.

## 🔧 Configuration

| Key | Default | Effect |
|---|---|---|
| `LDW["enabled"]` | `True` | Master switch; False keeps every replay on the TaskFlow path |
| `LDW["min_replays"]` | `3` | Replays before a plan stops using TaskFlow and compiles an LDW script |
| `extraction.flow_extraction_enabled` (Presets & Role → Advanced) | `True` | User-facing compile switch, global-only: `action=compile`, compile-on-miss and the repair recompile refuse while it is off. An EXISTING script still replays |
| `LDW["script_max_bytes"]` | `65536` | Cap for one `ldw__<plan_key>.py` |
| `LDW["journal_path"]` | `src/data/workflow_runs.db` | Cross-process journal location |
| `LDW["auto_degrade_enabled"]` | `True` | Create and drive a TaskFlow replay flow after an LDW failure |
| `LDW["repair_enabled"]` | `True` | Run repair extraction / recompile after a fallback failure |
| `LDW["real_replay_timeout_s"]` | `900.0` | Wall-clock ceiling for one LDW run |
| `LDW["max_failed_steps"]` | `5` | Refuse the fallback when more steps than this failed |
| `PTC["ptc_prefill_enabled"]` | `False` | Enables the optional PTC scripts consumed by LDW leaves |

## 🔒 Limits

- This is a replay accelerator, not a sandbox or a retry engine.
- LDW leaves inherit the role tool policy; the LDW sandbox/deepagents path is deliberately unused.
- The journal matches EXACTLY: `prompt` / `agent_type` / `model` / `schema` / `isolation` are hashed, and a changed variant re-executes. Fuzzy matching and whitespace/Unicode normalization of the prompt are deliberately absent — a step prompt can embed code or structured upstream results where whitespace is semantic, so a near-match cached result could seed downstream steps with the wrong output. A miss costs one real execution and writes the success back.
- A changed plan or variant produces a different journal key and re-executes; a stale script is never trusted over the current prompt.
- Side effects of a re-run failed step are not rolled back; the journal skips only steps that previously succeeded.
- Successful steps are never spawned again in a fallback. Failed steps use TaskFlow retry/judge behavior; there is no separate LDW retry loop.

## 🗺️ Tests

| Area | Tests |
|---|---|
| Vendored core import + DAG + journal + failure propagation | `tests/agent/tools/subagent/test_ldw_shim.py` |
| Metadata / deps / variant / schema prompt | `tests/agent/tools/ldw/test_compiler.py` |
| Script literals / round-trip / unsafe key | `tests/agent/tools/ldw/test_scripts.py` |
| Threshold / SQLite journal / auto-degrade result | `tests/agent/tools/ldw/test_replay.py` |
| Leaf tool face / schema / PTC success and fallback | `tests/agent/tools/ldw/test_leaf.py` |
| Fallback seeding / resume / journal write-back | `tests/agent/tools/ldw/test_taskflow_adapter.py` |
| Repair merge / persistence / recompile | `tests/agent/tools/ldw/test_repair.py` |
| Main-only tool surface and degraded status | `tests/agent/tools/ldw/test_tool.py` |
| Real TaskFlow + real model replay / second run / fallback / repair / live agent | `tests/full/test_ldw_real_plan_e2e.py` |
