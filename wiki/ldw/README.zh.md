# 🔁 LDW 重放

[English](README.md) · **中文** · [日本語](README.ja.md) · [한국어](README.ko.md)

> 为已验证的 TaskFlow plan 提供 workflow 级重放：把 step DAG 编译成结构化元数据，由 vendored [langchain-dynamic-workflow](https://github.com/OWNER/langchain-dynamic-workflow) 核心执行，并用内容哈希 journal 缓存。它与 [PTC](../ptc/README.md) 的工具调用级预跑互补。

事实来源：`vendor/ldw/**`、`agent/tools/ldw/**`、`agent/tools/subagent/ldw_shim.py`、`config/features/agent_side/ldw.py`、`tests/agent/tools/ldw/**`、`tests/agent/tools/subagent/test_ldw_shim.py`。

## 🎯 总览

LDW 重放是 TaskFlow 成功 plan 的**第二条执行路径**：

| 路径 | 粒度 | 缓存 | 触发 |
|---|---|---|---|
| TaskFlow | step / subagent | evidence ledger + registry | 正常计划执行 |
| PTC 重放 | step 内工具调用 | skill 脚本 | executor spawn 预跑 |
| LDW 重放 | step DAG | prompt + agent_type + model + schema 的 journal | replay 次数达阈值后的 `workflow_replay` 工具 |

LDW 核心 vendored 在 `vendor/ldw/`，导入时安装最小 `deepagents` stub。sherry 使用自己的 LangGraph leaf runnable，因此 LDW 的 sandbox/leaf 构造路径不会被调用。

## 🧭 触发与工具

主 agent 有一个 main-only 的 `workflow_replay` 工具：

```json
{"action": "status", "variant_input": "", "flow_id": "", "plan_key": "", "force": false}
```

- `status` 返回 replay 次数与脚本路径。
- `compile` 从当前 TaskFlow flow DAG 生成 `ldw__<plan_key>.py`。
- `replay` 递增计数；低于 `LDW["min_replays"]` 时继续走 TaskFlow，达到阈值才运行 LDW。

阈值是刻意的懒编译：低频 plan 不承担 LDW 初始化成本。默认第 3 次 replay 才从当前 TaskFlow flow 编译，不由 skill review 预先编译。

`variant_input` 是当前任务的具体输入，会进入每个 step prompt，因此进入 journal key；同一 plan 的不同变体不会命中旧缓存。step 后缀也属于 `agent_type`，因为 PTC 脚本是 step 级的。

## 🧱 编译与产物

`compile_plan_to_workflow(plan_knowledge)` 返回可 JSON 持久化的元数据：

```json
{"plan_key": "...", "flow_id": "...", "steps": [{"step_id": "step-1", "task_template": "...", "agent_type": "executor@step-1", "deps": [], "response_schema": null}]}
```

`build_orchestrate(meta, variant_input)` 构造 LDW `orchestrate`。每个 `DagNode` 接收前驱结果；prompt 带 `[Current task input]` 与 `[Previous step results]`，保证变体和上游输出不同时 journal key 不同。

产物放在 owning skill 的 `scripts/` 顶层：

```text
skills/auto/<owning-skill>/scripts/ldw__<plan_key>.py
skills/auto/<owning-skill>/scripts/ptc__<plan_key>__<step_id>.py
```

LDW 脚本把 `PLAN_KEY`、`REQUIRED_AGENT_TYPES`、`WORKFLOW_META` 存为模块常量；不存闭包、源码字符串或 `workflow.json` sidecar。

## ⚙️ 运行与 journal

- `register_roster(meta, session_id)` 为每个 step 构造 session-bound runnable（`agent_type` 含 step 后缀），并通过 runnable config 传 `session_id`。
- leaf 是按角色工具白名单构造的 LangGraph 图；session 不会被烘进共享图。
- journal miss 时 executor leaf 可先跑该 step 的 PTC 脚本（`run_ptc`）；PTC 失败是硬 gate，回退 ReAct。PTC 实现归 [PTC replay](../ptc/README.md) 所有。
- `SqliteWorkflowStore`（`src/data/workflow_runs.db`）支持跨进程 resume。同 prompt / agent_type / model / schema 的第二次 replay 零模型调用返回 journal 结果。
- 同一 plan 的 replay 用 per-plan 锁串行化。

## 🪜 降级与修补

LDW 没有 step retry/judge：

- 失败 DAG 节点为 `None`，下游 skip。
- 旧的“重跑 dag”L2 已删除。根 step 失败、失败率 >50% 或失败 step 有下游依赖时直接进入 **L2 TaskFlow 降级**；否则失败叶子可接受 partial。
- TaskFlow 降级只重跑失败 step；成功 step 通过 journal 跳过，成功结果写回 journal。
- 修补抽取把修补后的真实成功路径与 failed_branches 合并进 plan knowledge；满足条件时重编译 LDW 脚本。模块提供 `merge_knowledge`、`extract_react_repair_experience`、`extract_failure_experience`、`repair_extraction`。

## 🔧 配置

| 键 | 默认 | 作用 |
|---|---|---|
| `LDW["min_replays"]` | `3` | 编译 LDW workflow 前的 replay 次数 |
| `LDW["script_max_bytes"]` | `65536` | 单个 `ldw__<plan_key>.py` 上限 |
| `LDW["journal_path"]` | `src/data/workflow_runs.db` | 跨进程 journal 路径 |
| `PTC["ptc_prefill_enabled"]` | `False` | 是否启用 LDW leaf 消费的 PTC 脚本 |

## 🔒 限制

- 这是重放加速器，不是 sandbox 或 retry 引擎。
- LDW leaf 继承角色工具策略；LDW sandbox/deepagents 路径刻意不使用。
- plan / 变体变化会产生不同 journal key 并重新执行；旧脚本绝不压过当前 prompt。
- 前两次 replay 明确留在 TaskFlow，只有第三次及以后才付 LDW 成本。
- LDW 运行失败时工具返回 partial/error；调用方回退 TaskFlow，绝不把 skip 的 step 当成功。

## 🗺️ 测试

| 范围 | 测试 |
|---|---|
| vendored core 导入 + DAG + journal + 失败传播 | `tests/agent/tools/subagent/test_ldw_shim.py` |
| 元数据 / deps / 变体 prompt | `tests/agent/tools/ldw/test_compiler.py` |
| 阈值 / SQLite journal / compile-on-miss | `tests/agent/tools/ldw/test_replay.py` |
| 修补合并 / 抽取路径 | `tests/agent/tools/ldw/test_repair.py` |
| main-only 工具面 | `tests/agent/tools/ldw/test_tool.py` |
| 真实 main agent 调用 `workflow_replay` | `tests/full/test_ldw_replay_e2e.py` |
