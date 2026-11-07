# 🔁 LDW Replay

**English** · [中文](README.zh.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

> Workflow-level replay for verified TaskFlow plans. The step DAG is compiled into structured metadata, and the vendored [langchain-dynamic-workflow](https://github.com/OWNER/langchain-dynamic-workflow) core executes it with a content-hash journal. This is the step-level counterpart to [PTC](../ptc/README.md)'s tool-call-level prefill.

Source of truth: `vendor/ldw/**`, `agent/tools/ldw/**`, `agent/tools/subagent/ldw_shim.py`, `config/features/agent_side/ldw.py`, `tests/agent/tools/ldw/**`, and `tests/agent/tools/subagent/test_ldw_shim.py`.

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

- `status` reports the replay count and the compiled script path.
- `compile` builds `ldw__<plan_key>.py` from the current TaskFlow flow DAG.
- `replay` increments the replay count, then either stays on TaskFlow (below `LDW["min_replays"]`) or runs LDW.

The replay threshold is deliberately lazy: low-frequency plans never pay the LDW setup cost. A plan is compiled at the threshold (default 3) from its current TaskFlow flow, not eagerly by skill review.

`variant_input` is the current task's concrete input. It is injected into every step prompt and therefore enters the journal key; the same plan with a different variant does not hit the old cache. The step suffix is also part of `agent_type`, because PTC scripts are step-scoped.

## 🧱 Compilation and Artifacts

`compile_plan_to_workflow(plan_knowledge)` returns JSON-persistable metadata:

```json
{"plan_key": "...", "flow_id": "...", "steps": [{"step_id": "step-1", "task_template": "...", "agent_type": "executor@step-1", "deps": [], "response_schema": null}]}
```

`build_orchestrate(meta, variant_input)` builds the LDW `orchestrate` callable. Each `DagNode` receives its predecessors' results, and the prompt carries `[Current task input]` plus `[Previous step results]`; this is what keeps the content-hash journal correct across variants and upstream outputs.

Artifacts live at the top level of an owning skill's `scripts/`:

```text
skills/auto/<owning-skill>/scripts/ldw__<plan_key>.py
skills/auto/<owning-skill>/scripts/ptc__<plan_key>__<step_id>.py
```

The LDW script stores `PLAN_KEY`, `REQUIRED_AGENT_TYPES`, and `WORKFLOW_META` as module constants. It does not store closures, source strings, or `workflow.json` sidecars.

## ⚙️ Runtime and Journal

- `register_roster(meta, session_id)` builds one session-bound runnable per step (`agent_type` includes the step suffix) and passes `session_id` through the runnable config.
- Leaves are LangGraph graphs built with the role's tool allow-list; the session is never baked into a shared graph.
- On a journal miss an executor leaf may run the step's PTC script `run_ptc` first; PTC failure is a hard gate and falls back to ReAct. The PTC implementation is owned by the [PTC replay path](../ptc/README.md).
- `SqliteWorkflowStore` at `src/data/workflow_runs.db` backs cross-process resume. A repeat replay with the same prompt / agent_type / model / schema returns the journaled result with zero model calls.
- Per-plan replay locks serialize concurrent replay of the same plan.

## 🪜 Degradation and Repair

LDW has no step retry/judge gate:

- A failed DAG node is `None` and its dependents are skipped.
- The old “rerun the dag” L2 level is removed. Failures go directly to the **L2 TaskFlow degradation** when the root failed, the failure rate is over 50%, or a failed step has dependents; otherwise a failed leaf may be accepted as partial.
- TaskFlow degradation replays only the failed steps; successful steps are skipped via the journal, and successful TaskFlow results are written back.
- Repair extraction merges the actual post-repair success path and failed branches into plan knowledge, then recompiles the LDW script when the plan is eligible. The repair module exposes `merge_knowledge`, `extract_react_repair_experience`, `extract_failure_experience`, and `repair_extraction`.

## 🔧 Configuration

| Key | Default | Effect |
|---|---|---|
| `LDW["min_replays"]` | `3` | Replays before an LDW workflow is compiled |
| `LDW["script_max_bytes"]` | `65536` | Cap for one `ldw__<plan_key>.py` |
| `LDW["journal_path"]` | `src/data/workflow_runs.db` | Cross-process journal location |
| `PTC["ptc_prefill_enabled"]` | `False` | Enables the optional PTC scripts consumed by LDW leaves |

## 🔒 Limits

- This is a replay accelerator, not a sandbox or a retry engine.
- LDW leaves inherit the role tool policy; the LDW sandbox/deepagents path is deliberately unused.
- A changed plan / variant produces a different journal key and re-executes; a stale script is never trusted over the current prompt.
- The first two replays explicitly stay on TaskFlow; only the third and later pay the LDW cost.
- If the compiled LDW run fails, the tool reports a partial/error result; the caller falls back to TaskFlow rather than silently treating skipped steps as success.

## 🗺️ Tests

| Area | Tests |
|---|---|
| Vendored core import + DAG + journal + failure propagation | `tests/agent/tools/subagent/test_ldw_shim.py` |
| Metadata / deps / variant prompt | `tests/agent/tools/ldw/test_compiler.py` |
| Threshold / SQLite journal / compile-on-miss | `tests/agent/tools/ldw/test_replay.py` |
| Repair merge / extracted paths | `tests/agent/tools/ldw/test_repair.py` |
| Main-only tool surface | `tests/agent/tools/ldw/test_tool.py` |
| Live main agent calls `workflow_replay` | `tests/full/test_ldw_replay_e2e.py` |
