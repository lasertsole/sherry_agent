# 🔁 LDW 重放

[English](README.md) · **中文** · [日本語](README.ja.md) · [한국어](README.ko.md)

> 面向已验证 TaskFlow 计划的工作流级重放。步骤 DAG 会编译为结构化元数据，由 vendored 的 [langchain-dynamic-workflow](https://github.com/OWNER/langchain-dynamic-workflow) 核心以内容哈希 journal 执行。LDW 重放失败时会从同一份已验证 DAG 自动创建一个新的 TaskFlow flow，再把成功结果写回 journal。

事实来源：`vendor/ldw/**`、`agent/tools/ldw/**`、`agent/tools/subagent/ldw_shim.py`、`config/features/agent_side/ldw.py`、`tests/agent/tools/ldw/**`、`tests/full/test_ldw_real_plan_e2e.py` 和 `tests/agent/tools/subagent/test_ldw_shim.py`。

## 🎯 概览

LDW 重放是已经通过 TaskFlow 成功的计划的**第二条执行路径**：

| 路径 | 粒度 | 缓存 | 触发 |
|---|---|---|---|
| TaskFlow | step / subagent | evidence ledger + registry | 正常计划执行 |
| PTC 重放 | step 内的工具调用 | skill 脚本 | executor spawn 预填充 |
| LDW 重放 | step DAG | 按 prompt + agent_type + model + schema 键控的 journal | 达到重放阈值后的 `workflow_replay` 工具 |

LDW 核心整体 vendored 在 `vendor/ldw/`，导入时安装最小的 `deepagents` stub。Sherry 提供自己的 LangGraph leaf runnable，因此永远不会调用 LDW 的 sandbox/leaf 构造模块。

## 🧭 触发与工具

主 agent 有一个 main-only 的 `workflow_replay` 工具：

```json
{"action": "status", "variant_input": "", "flow_id": "", "plan_key": "", "force": false}
```

- `status` 返回重放次数、最小阈值、已编译脚本路径，以及当前 session 是否有可读取的 TaskFlow DAG。
- `compile` 从当前 TaskFlow flow DAG 生成 `ldw__<plan_key>.py`。
- `replay` 先增加重放计数，然后低于 `LDW["min_replays"]` 时继续走 TaskFlow，否则运行 LDW；LDW 部分失败会自动进入 TaskFlow 降级。
- 返回的 JSON 是结构化的：`status=ok|taskflow|degraded|partial|no_workflow|no_plan|disabled|error`，并带 `failed_steps`、`journal_hits`、`leaf_calls`、`degraded` 以及适用时的 repair 字段。`ok` 对 `ok`、`taskflow`、`degraded` 为真。

重放阈值刻意保持惰性：低频计划永远不用承担 LDW 的装配成本。计划在达到阈值（默认 3）时才从当前 TaskFlow flow 编译，而不是由 skill review 提前编译。

`variant_input` 是当前任务的具体输入。它会进入每个 step 的 prompt，因此也进入 journal key；同一计划换变体不会命中旧缓存。step 后缀也属于 `agent_type`，因为 PTC 脚本是按 step 隔离的。

## 🧱 编译与产物

`compile_plan_to_workflow(plan_knowledge)` 返回可 JSON 持久化的元数据：

```json
{"plan_key": "...", "flow_id": "...", "steps": [{"step_id": "step-1", "task_template": "...", "agent_type": "executor@step-1", "deps": [], "response_schema": null, "step_timeout_seconds": null, "retry_policy": null}]}
```

`build_orchestrate(meta, variant_input)` 构造 LDW 的 `orchestrate` 可调用对象。每个 `DagNode` 接收前驱结果，prompt 携带 `[Current task input]`、`[Hard constraints]` 以及 `[Previous step results]`；这保证内容哈希 journal 在变体和上游输出变化时仍然正确。带 schema 的 step 会调用 `ctx.agent(..., schema=...)`，因此 leaf 输出经过校验的结构化对象，而不是自由文本。

产物位于所属 skill 的顶层 `scripts/`：

```text
skills/auto/<owning-skill>/scripts/ldw__<plan_key>.py
skills/auto/<owning-skill>/scripts/ptc__<plan_key>__<step_id>.py
```

LDW 脚本把 `PLAN_KEY`、`REQUIRED_AGENT_TYPES` 和 `WORKFLOW_META` 存为 Python 模块常量。它不存闭包、源码字符串或 `workflow.json` 边车文件。写入器使用 Python 字面量而不是 JSON 文本，因为 `json.dumps` 会输出小写 `true` / `false` / `null`，包含 `additionalProperties: false` 的 schema 因此会让脚本无法加载。

## ⚙️ 运行时与 Journal

- `register_roster(meta, session_id)` 按**每个 step 一个 builder** 注册。builder 让 LDW 能请求绑定 schema 的 runnable；`agent_type` 里的 step 后缀让 journal 和 PTC 查找保持 step 级隔离。
- leaf 复用真实 child 契约：`_assemble_child_tools` 应用角色 allow-list，并注入 code intelligence、ast-grep 和仅 executor 的 PTC。session id 通过 runnable config 传递，绝不烘进共享图。
- leaf 的模型遵循角色 tier 和 `step_model` 覆盖。带 schema 的 leaf 强制关闭 thinking，与 child agent 结构化输出的最后一级一致。
- journal miss 时，executor leaf 可能先用 `run_ptc` 执行该 step 的 PTC 脚本。PTC 失败会追加 `[PTC Pre-run Progress]` 并继续 ReAct；PTC 的 schema miss 是失败，绝不当作成功。
- `src/data/workflow_runs.db` 上的 `SqliteWorkflowStore` 支持跨进程恢复。相同 prompt / agent_type / model / schema 的重复重放直接返回 journal 结果，leaf 调用为零。每次结果会报告 `journal_hits` 和 `leaf_calls`。
- 每个计划的锁串行化并发重放。`LDW["real_replay_timeout_s"]` 限制单次 LDW 运行；超时算失败并成为降级候选，而不是挂起。

## 🪜 自动 TaskFlow 降级

LDW 运行失败且 `LDW["auto_degrade_enabled"]` 打开时，适配器会从**同一份已验证 DAG** 重建执行。它从不 reset 或重跑旧 flow。

1. 创建一个新的 `ldw-<plan_key 前 8 位>-<8 位 uuid>` TaskFlow flow；原 flow 仍存在时带 `resumed_from=<origin>`。
2. 把每个非 None 的 LDW 结果预置为 DONE step，使用合成的 `ldw-journal:<flow>:<step>` child key 和结果记录；只有失败/未完成的 step 保持 ready/blocked。
3. 通过正常 TaskFlow 派发 ready step，因此 retry policy、schema gate、StepJudge 和 child 完成语义与正常执行完全一致。
4. 用 `taskflow_wait_all` 轮询，然后对每个 settled child 调用 `taskflow_resume`。超时或被杀 child 声明为 failure；`auto_dispatch` 启动下一波。
5. 全部完成的 flow 正常 finish。finish 被拒或仍有未完成工作时，将 flow 终结（fail 或 cancel），不留下活跃孤儿 flow。
6. 用相同的 prompt/agent_type/model/schema key 把 TaskFlow 成功结果写回 LDW journal，因此该变体的下一次 LDW 重放是零调用。

有 step 失败时，`repair_extraction` 读取 replay flow 的 child handoff，把真实成功路径和失败分支合并进计划知识，持久化 `success-steps.json`，并重新编译 LDW 脚本。Repair 是 fail-open：持久化或重编译错误会出现在结果里，但绝不会让重放本身崩溃。PTC 脚本归 skill-review 编译器所有；LDW repair 只重编译自己的产物。

## 🔧 配置

| 键 | 默认值 | 作用 |
|---|---|---|
| `LDW["enabled"]` | `True` | 总开关；False 让所有重放继续走 TaskFlow |
| `LDW["min_replays"]` | `3` | 计划停止走 TaskFlow 并编译 LDW 脚本前的重放次数 |
| `LDW["script_max_bytes"]` | `65536` | 单个 `ldw__<plan_key>.py` 的上限 |
| `LDW["journal_path"]` | `src/data/workflow_runs.db` | 跨进程 journal 位置 |
| `LDW["auto_degrade_enabled"]` | `True` | LDW 失败后创建并驱动 TaskFlow replay flow |
| `LDW["repair_enabled"]` | `True` | 降级失败后执行 repair 抽取 / 重编译 |
| `LDW["real_replay_timeout_s"]` | `900.0` | 单次 LDW 运行的墙钟上限 |
| `LDW["max_failed_steps"]` | `5` | 失败 step 超过该数时拒绝降级 |
| `PTC["ptc_prefill_enabled"]` | `False` | 启用 LDW leaf 消费的可选 PTC 脚本 |

## 🔒 限制

- 这是重放加速器，不是 sandbox，也不是重试引擎。
- LDW leaf 继承角色工具策略；LDW 的 sandbox/deepagents 路径刻意不用。
- 计划或变体变化会生成不同的 journal key 并重新执行；旧脚本绝不会压过当前 prompt。
- 重新执行的失败 step 的副作用不会回滚；journal 只跳过之前成功过的 step。
- 降级时成功 step 绝不会再次 spawn。失败 step 走 TaskFlow 的 retry/judge 行为；LDW 没有独立的重试循环。

## 🗺️ 测试

| 区域 | 测试 |
|---|---|
| Vendored 核心导入 + DAG + journal + 失败传播 | `tests/agent/tools/subagent/test_ldw_shim.py` |
| 元数据 / 依赖 / 变体 / schema prompt | `tests/agent/tools/ldw/test_compiler.py` |
| 脚本字面量 / 往返 / 不安全 key | `tests/agent/tools/ldw/test_scripts.py` |
| 阈值 / SQLite journal / 自动降级结果 | `tests/agent/tools/ldw/test_replay.py` |
| Leaf 工具面 / schema / PTC 成功与回退 | `tests/agent/tools/ldw/test_leaf.py` |
| 降级预置 / resume / journal 写回 | `tests/agent/tools/ldw/test_taskflow_adapter.py` |
| Repair 合并 / 持久化 / 重编译 | `tests/agent/tools/ldw/test_repair.py` |
| Main-only 工具面与 degraded 状态 | `tests/agent/tools/ldw/test_tool.py` |
| 真实 TaskFlow + 真实模型重放 / 二次运行 / 降级 / repair / 真实 agent | `tests/full/test_ldw_real_plan_e2e.py` |
