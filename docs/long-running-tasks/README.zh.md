# ⏳ 长时任务：TaskFlow、预算、截止时间、记忆与连续性

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> Agent 如何执行跨越多个回合的长期工作：一个持久化的 SQLite DAG 引擎（`taskflow_*`，共 14 个工具）跨对话回合跟踪有依赖关系的步骤，将每个步骤派发给一个分离的子 Agent 执行，按可选策略自动重试失败或死亡步骤，用辅助 LLM 判别携带验收标准的步骤结果（`pass` / `retry` / `block`），按预算聚合 token/成本开销，由后台 sweeper 让超期或空闲的 flow 过期，提供按会话隔离的 flow 面板（所有读取都在 SQL 层按所属会话过滤；子 Agent 永远不会拿到 taskflow/todolist/knowledge 工具），并通过两层记忆系统、压缩前的记忆落盘、摘要与 TaskFlow 的桥接、工具输出单行摘要、跨会话连续性、子 Agent 完成时的记忆回流，以及把活动 flow 自动注入系统提示词，把上下文一路传递下去。

事实来源：`agent/tools/taskflow/**`、`agent/tools/memory.py`、`agent/middlewares/summarization/memory_flush.py`、`agent/middlewares/summarization/core.py`（TaskFlow 上下文区块）、`agent/middlewares/subagent_completion_drain/core.py`（记忆回流）、`agent/middlewares/task_intent/core.py`、`agent/middlewares/todo_continuation/core.py`、`context_engine/session_continuity.py`、`workspace/prompt_builder.py`、`pub/func/message/tool_output_prune.py`、`agent/tools/subagent/registry/sweeper.py`、`agent/wrapper/**`、`config/features/**`。下文中的每一处常量、签名与行号都已对照这些代码逐一核实。

## 目录

- [概览](#-概览)
- [TaskFlow 引擎](engine/README.zh.md)
  - [TaskFlow DAG 引擎](engine/README.zh.md#-taskflow-dag-引擎)
  - [步骤重试策略](engine/README.zh.md#-步骤重试策略)
  - [Token / 成本预算](engine/README.zh.md#-token--成本预算)
  - [任务截止时间](engine/README.zh.md#-任务截止时间)
  - [结果校验](engine/README.zh.md#-结果校验)
  - [进度报告](engine/README.zh.md#-进度报告)
  - [空闲检测](engine/README.zh.md#-空闲检测)
  - [会话面板与隔离](engine/README.zh.md#-会话面板与隔离)
- [记忆与连续性](memory/README.zh.md)
  - [分层记忆](memory/README.zh.md#-分层记忆)
  - [压缩前的记忆落盘](memory/README.zh.md#-压缩前的记忆落盘)
  - [摘要 ↔ TaskFlow 协调](memory/README.zh.md#-摘要--taskflow-协调)
  - [子 Agent 记忆回流](memory/README.zh.md#-子-agent-记忆回流)
  - [工具输出摘要](memory/README.zh.md#-工具输出摘要)
  - [会话连续性](memory/README.zh.md#-会话连续性)
  - [TaskFlow 自动恢复](memory/README.zh.md#-taskflow-自动恢复)
- [并发 Lane](lanes/README.zh.md)
- [配置注册表](#-配置注册表)
- [架构图](#-架构图)
- [API 参考](#-api-参考)
- [测试](#-测试)
- [已知局限](#-已知局限)

## 🎯 概览

长时任务栈让主 Agent 能把一个多回合的任务分解为一个**持久化 flow**，其步骤之间可以互相依赖，把每个就绪步骤派发给子 Agent 执行，并在进程重启后继续存在。共有六个子系统协作：

| # | 子系统 | 入口 | 持久化位置 |
| :- | :--- | :--- | :--- |
| 1 | **TaskFlow DAG 引擎** | `agent/tools/taskflow/` | `data/taskflow_registry.db`（SQLite，WAL） |
| 2 | **Token / 成本预算** | `taskflow_budget`、`taskflow_resume` | `task_flows.total_tokens` / `total_cost` / `token_budget` |
| 3 | **截止时间** | `taskflow_create(deadline_hours=…)` + sweeper | `task_flows.deadline_ts` |
| 4 | **空闲检测** | sweeper `_scan_stale_waiting_taskflows` | `wait_json` 中的过期标记 |
| 5 | **压缩前落盘** | `agent/middlewares/summarization/memory_flush.py` | `workspace/memory/MEMORY.md` |
| 6 | **连续性与自动恢复** | `context_engine/session_continuity.py`、`workspace/prompt_builder.py` | `src/data/session_continuity/*.json` + 提示词区块 |

贯穿始终的设计契约是**错误即文本**：工具从不把业务错误抛给模型，而是返回以 `Error:` 开头的人类可读字符串。每个后台钩子都是**失败开放（fail-open）**的——注册表不可用或 sweeper 崩溃只会退化为“没有长时任务上下文”，绝不会破坏一个回合。

## ⚙️ 配置注册表

所有可调项都放在 `config/features/` 下，它是一个**按对象划分的 `TypedDict` 包**——而不是单个庞大的模块。它分为三部分：

| 部分 | 内容 |
| :--- | :--- |
| `config/features/agent_side/` | **26** 个 Agent 侧配置模块（中间件、工具、LLM 客户端、记忆、TaskFlow） |
| `config/features/infra_side/` | **19** 个基础设施侧配置模块（服务端、队列、技能、上下文引擎、运行时、模型定价） |
| `config/features/_env.py` | 唯一的共享环境辅助函数 |

每个模块定义一个 `class XxxConfig(TypedDict)` 以及一个模块级常量 `XXX: XxxConfig = {…}`。感知环境的模块定义一个构建函数 `def _build_xxx(env: Mapping[str, str] | None = None) -> XxxConfig`，它读取 `env or os.environ`，并在导入时物化常量。环境辅助函数是 `_env_int(name, default, env)`（`config/features/_env.py:9`），它接受 `1/true/yes/on` 与 `0/false/no/off/""`，并且从不抛异常。

该注册表当前包含 **45 个 feature 对象**——Agent 侧 26 + 基础设施侧 19——通过各包的 `__init__.py` 重新导出，并由 `config/features/__init__.py` 汇总，因此消费方可以从单一位置导入其中一半或整个注册表。消费方代码直接导入常量并索引它（例如 `ITERATION_BUDGET["default_max_iterations"]`）；不存在 `get_feature`/`load_feature` 访问器。`config/__init__.py:38-39` 从 `GATEWAY` 派生出 `API_HOST`/`API_PORT`。

与本文档最相关的常量：

| 配置对象 | 字段 | 值 |
| :--- | :--- | :--- |
| `TASKFLOW_INFRA`（`agent_side/taskflow_infra.py`） | `busy_timeout_ms` | 5000 |
| | `init_wait_timeout_s` | 10.0 |
| | `persist_max_attempts` | 3 |
| | `wait_all_min_poll_interval_seconds` | 0.05 |
| | `wait_all_default_timeout_seconds` | 300.0 |
| | `wait_all_default_poll_interval_seconds` | 0.5 |
| | `waiting_timeout_hours` | 24 |
| `MODEL_PRICING`（`infra_side/model_pricing.py`） | `model_pricing_per_m_tokens` | `glm-5` / `deepseek-chat` / `kimi-latest` / `_default` |
| | `budget_warn_threshold` | 0.80 |
| `MEMORY_FLUSH`（`agent_side/memory_flush.py`） | `enabled` | `MEMORY_FLUSH_ENABLED`（默认 1） |
| | `model` | `MEMORY_FLUSH_MODEL`（默认 ""） |
| | `soft_threshold_tokens` | 8000 |
| | `force_flush_chars` | 50000 |
| | `output_max_tokens` | 2048 |
| | `timeout_seconds` | 30 |
| `SUMMARIZATION`（`agent_side/summarization.py`） | `prune_protect_tokens` | 40000 |
| | `prune_min_reduction_tokens` | 5000 |
| | `protected_tools` | `{"memory", "skill_view", "skill_list"}` |
| `MES_MEMORY`（`infra_side/mes_memory.py`） | `busy_timeout_s` / `connect_attempts` | 10.0 / 5 |

## 🏗️ 架构图

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

编译后的图由 **`agent/wrapper/`** 包包装，该包拥有这些守卫。`agent.wrapper.registry` 暴露一条进程级、有序、可插拔的链（`register_graph_wrapper`、`unregister_graph_wrapper`、`apply_graph_wrappers`、`reset_graph_wrappers`），其 `GraphWrapperFactory` 条目按**最内层优先**应用；默认项先是 `RepetitionGuardWrapper(phantom_stream_guard=True)`，再是 `ContextLimitGuardWrapper(context_window=main_llm_max_tokens)`。流式重复守卫位于 `agent/wrapper/repetition_guard.py`，上下文窗口守卫位于 `agent/wrapper/context_limit.py`。**记忆回流**由 `agent/middlewares/subagent_completion_drain/core.py` 中的 `SubagentCompletionDrainMiddleware` 执行。

## 📚 API 参考

### TaskFlow 工具

| 工具 | 签名 | 返回 |
| :--- | :--- | :--- |
| `taskflow_create` | `(flow_id, description="", initial_state=None, session_id, deadline_hours=None)` | 创建的 id/状态/版本（+ 截止时间） |
| `taskflow_run_task` | `(flow_id, task, label=None, expected_revision=None, depends_on=None, validation_criteria=None, retry_policy=None, session_id)` | 已派发步骤，或带待满足依赖的 `blocked` |
| `taskflow_dispatch` | `(flow_id, step_ids, expected_revision=None, session_id)` | 已派发的 step id + 版本 |
| `taskflow_wait_all` | `(flow_id, timeout_seconds=300.0, poll_interval_seconds=0.5, session_id)` | 每个步骤的落定报告（完整或部分；对策略步骤自动重试） |
| `taskflow_resume` | `(flow_id, child_session_key="", result="", expected_revision=None, token_usage=None, validation_criteria=None, session_id)` | 恢复后的状态、解锁的步骤、步骤状态计数、标准回显、判别器判定、重试说明 |
| `taskflow_set_waiting` | `(flow_id, wait_reason="", expected_revision=None, session_id)` | waiting 状态 + 版本 |
| `taskflow_summary` | `(flow_id, session_id)` | 完整 flow 状态，含等待/截止状态 |
| `taskflow_progress` | `(flow_id, session_id)` | 完成度 %、分解、后续步骤、预计剩余 |
| `taskflow_budget` | `(flow_id, action="query", token_budget=None, expected_revision=None, session_id)` | 预算报告，或设置确认 |
| `taskflow_list` | `(status_filter="active", session_id)` | 本会话面板（`active` / `all` / 状态名） |
| `taskflow_finish` | `(flow_id, summary="", expected_revision=None, todo=None, plan_path=None, checkbox_label=None, session_id)` | 终态 `done`（四道门、失败开放） |
| `taskflow_fail` | `(flow_id, reason="", expected_revision=None, session_id)` | 终态 `failed` |
| `taskflow_cancel` | `(flow_id, reason="", expected_revision=None, session_id)` | 终态 `cancelled` |

### memory 工具动作

| 动作 | 签名 | 返回 |
| :--- | :--- | :--- |
| `add` / `replace` / `remove` | `memory(action, target="memory"\|"user", content, old_text)` | JSON 成功/错误 |

### 关键函数与常量

| 符号 | 位置 | 作用 |
| :--- | :--- | :--- |
| `TaskFlowStatus` / `StepStatus` | `agent/tools/taskflow/config.py:11,21` | 生命周期 / DAG 枚举 |
| `update_flow` | `agent/tools/taskflow/registry/store_sqlite.py` | 乐观锁的会话作用域变更 |
| `get_active_flows_sync` | `agent/tools/taskflow/registry/store_sqlite.py` | 会话作用域活动 flow 读取（SQL `session_id` 过滤） |
| `get_overdue_flows` / `get_waiting_flows` | `store_sqlite.py` | sweeper 查询（刻意跨会话） |
| `deps_satisfied` / `unlock_dependents` | `agent/tools/taskflow/tools/_shared.py:115,153` | DAG 状态转换 |
| `update_flow_with_conflict_retry` | `_shared.py:191` | 绝不丢失已派子 Agent 的持久化 |
| `_expire_overdue_taskflows` | `agent/tools/subagent/registry/sweeper.py:123` | 截止时间执行 |
| `_scan_stale_waiting_taskflows` | `sweeper.py:154` | 空闲检测标记 |
| `_get_taskflow_context_sync` | `agent/middlewares/summarization/core.py:122` | 摘要协调 |
| `_build_taskflow_block` | `workspace/prompt_builder.py:145` | 自动恢复提示词区块 |
| `prune_tool_outputs` | `pub/func/message/tool_output_prune.py:104` | 工具输出单行摘要 |
| `auto_save_on_session_end` | `context_engine/session_continuity.py:117` | 连续性保存钩子 |
| `should_flush` / `run_memory_flush` | `agent/middlewares/summarization/memory_flush.py:43,65` | 压缩前落盘 |
| `append_entries` | `agent/tools/memory.py:281` | 批量追加 MEMORY.md |
| `get_all_flows_sync` | `agent/tools/taskflow/registry/store_sqlite.py` | 会话面板读取（SQL `session_id` 过滤） |
| `classify_failure` / `should_retry_failure` | `agent/tools/taskflow/tools/_retry.py:56,103` | 失败分类 |
| `plan_settled_retries` / `persist_retry_actions` | `agent/tools/taskflow/tools/_retry.py:218,273` | wait_all 重试规划/持久化 |
| `_backflow_shared_memory` | `agent/middlewares/subagent_completion_drain/core.py:93` | 记忆回流对账 |
| `judge_step_result` | `agent/tools/taskflow/step_judge.py:142` | 步骤级 pass/retry/block 判定 |
| `collect_evidence_summary` | `agent/tools/taskflow/evidence_collector.py:22` | 判别器提示词证据摘要 |
| `apply_graph_wrappers` | `agent/wrapper/registry.py:69` | 可插拔图包装链 |

### 合成聚合（`aggregate_deps`）

`taskflow_run_task(aggregate_deps=true)` 标记一个合成步骤，其依赖结果会以 `## Upstream Results` 区块追加到派发任务中。`build_task_with_dep_results(step, steps, results)` 按 `depends_on` 顺序，将每个依赖的 `child_session_key` 与 flow 的 `{child_session_key, result, result_hash}` 记录匹配；结果缺失时回退为 `no result recorded` 占位符。聚合会在每次重派时从稳定结果重新推导，因此已存储的步骤任务永不被改写；省略该标志则保持旧有派发文本不变。

## 🧪 测试

TaskFlow 测试位于 `tests/agent/tools/taskflow/`（二十四个 `unit` 测试文件加一个共享的 `conftest.py`）：

| 测试文件 | 覆盖内容 |
| :--- | :--- |
| `test_store_sqlite.py` | CRUD、版本自增、乐观并发冲突、WAL、同步访问器、active/waiting/terminal 过滤 |
| `test_step_graph.py` | `deps_satisfied`、`mark_step_done`、`unlock_dependents`、旧状态推导、自依赖保护 |
| `test_summary_dag.py` | `taskflow_summary` 的 DAG 渲染 |
| `test_resume_dag.py` | 恢复标记 done、解锁后继、部分完成、幂等空操作 |
| `test_taskflow_tools.py` | 跨重启的完整 create→run→resume→finish、冲突、终态转换 |
| `test_taskflow_dispatch.py` | 批量派发、全有或全无校验、批次中途失败的持久化 |
| `test_dag_e2e.py` | 跨进程重启的完整并行 DAG flow |
| `test_conflict_persistence.py` | 冲突后已派子 Agent 的持久化、重试耗尽 |
| `test_taskflow_wait_all.py` | flow 范围等待、超时部分报告、无关的存活子 Agent |
| `test_run_task_dag.py` | 阻塞注册、满足后派发、未知依赖错误 |
| `test_taskflow_progress.py` | 完成度/分解/后续步骤/预计剩余/waiting |
| `test_token_budget.py` | token 聚合、成本计算、预算查询/设置/警告/超限 |
| `test_deadline.py` | `deadline_hours`、摘要渲染、sweeper 过期 |
| `test_idle_detection.py` | active/stale 等待状态、sweeper 标记、存活子 Agent 跳过 |
| `test_retry_policy.py` | 策略校验、失败分类、重新派发、耗尽 |
| `test_validation.py` | 标准存储、判别器判定（`pass`/`retry`/`block`）、覆盖、失败开放 |
| `test_step_judge.py` | StepJudge 响应解析、判定归并、失败开放降级为 `pass`、提示词组装 |
| `test_resume_with_judge.py` | `taskflow_resume` + 判别器：pass/retry/block、重试反馈注入、预算耗尽、失败开放 |
| `test_evidence_collector.py` | 面向判别器提示词渲染证据摘要；staleness 由更晚的 `stale` 行推导 |
| `test_finish_gate.py` | 完成门 A–D：DAG 完整性、blocked 步骤、失败/stale 证据、`SisyphusVerifier` |
| `test_chain_smoke.py` | 判别器 → 重试反馈 → goal loop → 完成门的集成冒烟（stub 辅助 LLM） |
| `test_taskflow_list.py` | 会话面板渲染、状态过滤、最后活动时间戳 |
| `test_index_audit.py` | SQLite 索引升级路径与查询计划审计 |
| `test_update_steps.py` | 步骤列表全量替换：增删/重写/重排与 dispatched/done 安全规则 |

跨领域测试套件：`tests/agent/middlewares/test_memory_flush.py`（落盘阈值与 `append_entries`）、`tests/agent/middlewares/test_lt5_memory_backflow.py`（完成排空时的记忆对账）、`tests/agent/middlewares/test_subagent_completion_drain_reminder.py`（完成载体校验提醒）、`tests/agent/middlewares/test_completion_drain_gate.py`（可选 `enforce_verification` 程序化门控）、`tests/agent/tools/subagent/test_completion_judge.py` + `test_goal_loop.py`（完成判别器与有界 goal loop）、`tests/agent/tools/test_evidence_auto_record.py` + `test_evidence_stale.py` + `tests/agent/tools/todolist/test_evidence_ledger.py`（证据记录、stale 事件、账本视图）、`tests/context_engine/test_session_continuity.py`（连续性保存/提示词）、`tests/agent/middlewares/test_todo_continuation.py`（回合结束续跑）、`tests/pub/func/message/test_tool_output_prune.py`（单行摘要）、以及 `tests/workspace/test_prompt_builder_taskflow.py`（待处理 flow 的提示词注入）。

用标准的 uv/pytest 工具只跑这一区块：

```bash
uv run pytest tests/agent/tools/taskflow -q
uv run pytest tests/agent/middlewares/test_memory_flush.py tests/context_engine/test_session_continuity.py -q
uv run pytest tests/pub/func/message/test_tool_output_prune.py -q
```

要跑完整的进程隔离套件，使用 `uv run python tests/run_tests_split.py`（Group A 跑 `unit` 文件，Group B 跑 `module`/`integration` 文件）。

## ⚠️ 已知局限

- **`done` 不等于成功。** 步骤的 `done` 只表示“已注入结果”；不存在 `failed`/`skipped` 步骤状态。即使子 Agent 报告错误，`taskflow_resume` 仍会把步骤标记为 `done` 并解锁后继。感知失败的步骤转换被有意推迟。
- **`taskflow_wait_all` 按设计限定于单个 flow。** 它只等待记录在给定 flow 的已派发步骤上的子 Agent，未知/已清理的 run 计入已落定。不存在“等待所有活动 flow”的全局原语。
- **空闲检测是提示性的。** sweeper 会把 `stale_detected_at` / `stale_child_session_key` 写入 `wait_json`，但从不自动把过期 `waiting` flow 置为失败；需要人或模型对该标记采取行动。
- **压缩前落盘处于潜伏状态。** `Summarization` 的生产实例（主 Agent 与子 Agent）未传入 `memory_store` / `llm_factory`，因此在某个调用点接线之前落盘不会运行；代码已实现并有测试，但目前不生效。
- **连续性依赖渠道。** `build_continuity_prompt` 同时需要 channel id 与 chat id，因此没有渠道绑定的会话拿不到连续性区块。存储是磁盘上按 key 划分的 JSON，而不是数据库。
- **三处重复的活动 flow 扫描。** `prompt_builder._build_taskflow_block`、`summarization._get_taskflow_context_sync` 与 `session_continuity._get_active_taskflow_ids_sync` 各自独立实现了同一查询；必须保持同步。
- **注册表规模是 45。** 配置注册表包含 45 个 feature 对象（Agent 侧 26 + 基础设施侧 19）；基础设施侧契约测试覆盖其中 18 个（GATEWAY 加 17 个数据驱动用例），遗漏了 `MODEL_PRICING`。
- **包导出缺口。** `agent/tools/taskflow/__init__.py` 只重新导出十一个名字；`taskflow_dispatch` 与 `taskflow_wait_all` 可通过 `build_taskflow_tools()` 获取，但被包 `__all__` 遗漏。
- **TaskFlow 区块仅限 LLM 提示词。** LLM 失败时使用的确定性回退摘要不包含 `## Current TaskFlow State`。
- **Token 记账由调用方提供。** 只有当 `taskflow_resume` 收到 `token_usage` 字典时才计算成本；未提供时注入的步骤贡献零 token 与零成本。
- **有标准时结果校验由判别器执行。** 携带标准的步骤在恢复时由辅助 LLM 步骤判别器审查：`retry` 会在步骤自身重试预算内重新派发；`block`（或预算耗尽）会把步骤标记为 `blocked`。判别器失败开放，因此禁用/不可用时降级为 `pass`——并不存在“结果一定满足标准”的硬保证，只有在步骤被接受前的一次尽力而为判定。
- **重试分类基于文本。** `classify_failure` 是对结果文本的子串启发式：措辞不在模式表内的失败（或被否定措辞掩盖的真实失败）不会触发重试，而空的 `retry_on` 会重试所有可分类失败。`taskflow_wait_all` 无法对没有结果文本的死亡子 Agent 分类，因此只要预算尚存它就会消耗重试预算。
- **`taskflow_list` 是会话作用域的。** 不存在全局跨会话面板：所有读取都在 SQL 层按所属 `session_id` 过滤，因此一个会话无法枚举另一个会话的 flow。隔离前写入的行（`session_id = ''`）对会话读取不可见，但 sweeper 的跨会话截止/空闲扫描仍能看到它们。
- **知识存储以计划身份为键，而非计划名。** 访问由归属校验把关（`ownership.is_plan_associated()`：本会话的 `plan_ref`、todo 的 `plan_ref`，或 `session_ids` 含本会话的 boulder work），存储目录由规范化计划路径派生——`workspace/knowledge/plans/<plan_key>/`，其中 `plan_key = sha1(相对仓库根的路径)[:12]`，目录内的 `meta.json` 记录可读的 `plan_name` / `plan_ref`。**计划文件不同**的同名计划在两个会话中**物理隔离**（各自写自己的 key 目录）；通过 boulder `session_ids` 协同**同一计划文件**的会话解析出同一路径，共享同一目录。计划文件无法解析的会话写入兜底身份 `session-<sha1(session_id)[:8]>`（全 id 哈希让前 8 字符相同的会话 id 不再相撞）。旧的名称为键目录保持可读；写入总是落在 key 目录；`clear_session` 删除本会话私有身份目录，保留与其他会话共享的计划。子 Agent 边界依旧绝对——`knowledge` 是 `main_only`。
