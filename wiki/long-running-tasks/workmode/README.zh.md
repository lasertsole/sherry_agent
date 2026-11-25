# 🛠️ WorkMode 子机制 — 任务级可靠性与协调

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> 属于[长任务](../README.zh.md)的一部分：五个可选机制，加固运行中的 TaskFlow，抵御中途打断、静默卡死、低质量步骤、重复规划与任务事实缺失。它们借鉴 WorkMode 式长任务体系的**子机制**，而不复制其三层组织；专家团模式回答"谁来做"，这些机制回答"怎么可靠地做"。

**代码真源：** `agent/middlewares/task_intent_router/`、`agent/tools/taskflow/watchdog.py`、`agent/tools/taskflow/step_judge.py`、`agent/tools/taskflow/template_store.py`、`agent/tools/taskflow/tools/`、`agent/tools/subagent/spawn/system_prompt.py`、`config/features/agent_side/taskflow_infra.py`、`config/path.py`。下面的每个常量与代码路径都经过核对。

## 五个机制一览

| 机制 | 解决的问题 | 所在位置 | 默认 |
| :--- | :--- | :--- | :--- |
| 意图分类输入路由 | 长任务中途的用户消息不应盲目打断任务 | `agent/middlewares/task_intent_router/` 的 before-agent 中间件 | 仅有活跃 flow 时启用；fail-open |
| 看门狗 + 死循环检测 | 卡死或空转的 flow 无人发现 | `agent/tools/taskflow/watchdog.py`，由子代理注册表 sweeper 扫描 | 只告警，不改 flow 状态 |
| 打分自评重做循环 | 二值 judge 没有质量梯度与重试上限 | `step_judge.py::judge_step_self_eval` + resume 消费 | 逐步 opt-in（`self_evaluate`） |
| 计划模板复用 | 同类任务每次都从零重新分解 | `agent/tools/taskflow/template_store.py`、`taskflow_flow` action | opt-in action |
| plan/environment 双文件 | 任务事实（路径/技术栈/约束/凭证引用）散落在对话里 | flow `state["environment"]` → 子代理系统提示词 | 空 = 提示词不变 |

五个机制全部是**追加式**的：没有活跃 flow、没有 `self_evaluate`、没有模板 action、没有 `environment` 时，行为与之前逐字节一致。

---

## 1. 意图分类输入路由

TaskFlow 运行时，新用户消息先由 `agent/middlewares/task_intent_router/intent.py` 的 `classify_turn_intent()` 分类。它是 **rules-only**——不调 LLM——沿用现有 TaskIntent 的四语言关键词匹配：中、英、日、韩的取消词都能识别。无法判定的输入解析为 `CONTINUE`（fail-open：绝不丢消息、不误路由）。

优先级：`CANCEL` > `SOFT_PREEMPT` > `RESUME` > `CONTINUE` > `NEW`。

| 意图 | 触发 | 路由 |
| :--- | :--- | :--- |
| `CANCEL` | 明确的停止词（"停下"、"cancel"、"やめて"、"중단"） | 通过 `taskflow_flow(action="cancel")` 取消所有活跃 flow，注入简短提示 |
| `SOFT_PREEMPT` | "等一下 / 调整方向"类措辞 | 给下一 turn 注入 advisory steering；不取消、不打断正在执行的 step |
| `RESUME` / `CONTINUE` | HITL 回答或任务补充信息 | 空操作——走现有 turn/HITL 路径 |
| `NEW` | 明显无关的新话题 | advisory steering；服务端排队输入机制仍是权威队列 |

`TaskIntentRouterMiddleware` 在 `agent/middlewares/specs.py` 中注册为 main 位置 30（`ORDER IS A CONTRACT`），紧挨可选的 `DelegationPressureGate` 之前。**无活跃 flow 时空操作**，短任务永不进入此路径。由于 agent 层不能 import server 队列，SoftPreemption 与 New 通过向 turn 注入 advisory steering 实现，而非直接操作服务端排队输入。内部/系统指令消息不会被再次分类（取消提示不会触发第二次取消）。

---

## 2. 看门狗 + 死循环检测

`agent/tools/taskflow/watchdog.py` 为现有的调用级防护（InvalidToolCallRepair、LLMRetry、MaxTokensBoost）补上任务级存活性检测。子代理注册表 sweeper 在 liveness 之后调用 `sweep_and_notify()`。检测是纯 **fail-open**：不写任何 flow 状态，只推送 `system.notice` 告警（`source="taskflow:watchdog"`），由 agent 或用户决定取消、重试还是继续。

`WatchdogAlert(flow_id, session_id, kind, detail)` 的 kind：

- **心跳过期**（`heartbeat_timeout_s`，默认 600 秒）：flow 非终态且 `updated_at` 早于阈值。
- **无进展**（`deadlock_no_progress_s`，默认 1800 秒）：flow 长时间 running 且无 step 状态变化。
- **工具循环**（`deadlock_tool_fail_count`，默认 3）：同一 step/工具以相同参数反复失败。
- **judge 循环**（`deadlock_judge_fail_count`，默认 5）：某 step 的 judge 反复判 retry。

所有阈值在 `config/features/agent_side/taskflow_infra.py`。阈值设为 `0` 即禁用该项。通知失败会被吞掉——告警通道故障不会阻塞 sweeper。

---

## 3. 打分自评重做循环

`StepJudge` 的默认路径不变：二值 `PASS` / `RETRY` / `BLOCK` / `UNKNOWN` 裁决，`UNKNOWN`（judge 不可用）fail-closed。在此之上，`step_judge.py::judge_step_self_eval` 为声明了 `self_evaluate: true` 的 step 增加**可选打分路径**：

- 严格的 reviewer LLM 按 step 的 `judge_criteria` / `validation_criteria` 打 1–10 分，返回 `{"score", "reason", "feedback"}`。
- 分数 ≥ `self_eval_min_score`（默认 7）→ step `DONE`。
- 分数低于阈值且还有重试预算（`self_eval_max_retries`，默认 5）→ `RETRY`，并把分数与反馈追加到 step。
- 预算耗尽 → `forced_pass: true` 并把 step 标为 `DONE`，主观打分永远不会无限循环。
- judge 不可用返回 `score=None`，遵循配置的 `on_judge_error` 策略——绝不静默通过。

三个 step 字段（`self_evaluate`、`self_eval_min_score`、`self_eval_max_retries`）在 `run_task` 的 `TaskflowStepInput` schema 与 `_OPTIONAL_STEP_FIELDS` 白名单中。未声明 `self_evaluate` 的 step 走原有二值 judge 路径，完全不变。

---

## 4. 计划模板复用

`agent/tools/taskflow/template_store.py` 保存已完成 flow 的**分解结构**——step id、任务、依赖、功能角色、judge/validation 标准与 response schema——到 `APP_HOME/taskflow_templates/<name>.json`（`config.path.TASKFLOW_TEMPLATES_DIR`）。运行时字段（子会话 key、结果、revision、时间戳）经白名单剥离，所以模板是待重新执行的空壳，而非上次运行的重放。

两个 `taskflow_flow` action 暴露它：

- `save_as_template(flow_id, name, description="")` —— 原子写入 flow 的 step 结构（文件锁 + `tempfile` + `os.replace`）。
- `load_from_template(name, flow_id, description="")` —— 用模板的 step 结构创建新 flow 并绑定其 todo 根。

这与 PTC/LDW replay 明确**互补**：模板复用的是"任务怎么分解"；PTC/LDW 复用的是"确切的执行调用序列"。一个任务可以先 load 模板，再让预编译脚本重放调用。

> 渐进式 step 展开**不采用**。Sherry 的嵌套 TaskFlow 已覆盖：顶层 flow 保留粗粒度 step，step 的子 ORCHESTRATOR 自建子 flow；进度通过 `progress_push` / `system.notice` 上报。给 DAG 状态机加 `expandable → expanded` 转换不会带来上下文压缩收益（step 存在 SQLite 而非对话中），还要为部分展开增加 finish 门，得不偿失。

---

## 5. plan/environment 双文件

flow 创建时可带 `taskflow_flow(action="create", environment={...})`。该字典逐字存入 `state["environment"]`（`taskflow_create.py`）——凭证保持引用（`keychain://...`），绝不解析。`store_sqlite.get_flow_environment_sync(flow_id)` 按全局唯一 flow id 读回。

step 派发时，`_dispatch.dispatch_child()` 优先使用显式传入的 `environment`，否则读取所属 flow 的 environment，再传给 `spawn_subagent_direct()`。`SpawnRequest.environment` 把它带到 `SubagentPromptContext`，`agent/tools/subagent/spawn/system_prompt.py` 的 `_render_environment_section()` 在角色指令**之后**、规则**之前**渲染 `## Task Environment` 块。非字符串值以 JSON 渲染；当任一值含 `credential_ref` 时，该块提醒子代理这些是需用批准的凭证工具解析的引用，而非可直接使用的秘密。

保证：

- environment 为空或缺失 → **完全没有该 section**，现有子代理提示词逐字节不变。
- environment 读取失败 → 派发照常进行（fail-open），子代理只是没有 environment 块。
- 派发时显式 `environment` 覆盖 flow 级值。
- environment 是任务级的，与会话级 `project_dir` / `AGENTS.md` 注入正交。

---

## 配置参考

| 配置（`TASKFLOW_INFRA`） | 默认 | 用途 |
| :--- | :--- | :--- |
| `heartbeat_timeout_s` | `600.0` | 心跳过期告警阈值 |
| `deadlock_tool_fail_count` | `3` | 同工具连续失败计数 |
| `deadlock_judge_fail_count` | `5` | judge 连续 retry 计数 |
| `deadlock_no_progress_s` | `1800.0` | 无进展告警阈值 |

## 测试

- `tests/agent/middlewares/test_task_intent_router.py` —— 四语言分类、活跃 flow 空操作、取消路由、内部消息处理。
- `tests/agent/tools/taskflow/test_watchdog.py` —— 各类告警、阈值、终态过滤、通知 fail-open。
- `tests/agent/tools/taskflow/test_self_eval.py` —— 分数解析、通过/重做/强制通过、不可用策略、默认 judge 不受影响。
- `tests/agent/tools/taskflow/test_template_store.py` —— 结构白名单、原子 CRUD、合并 action。
- `tests/agent/tools/taskflow/test_environment.py` —— 持久化、同步读取、派发注入/优先级/fail-open。
- `tests/agent/tools/subagent/spawn/test_system_prompt_environment.py` —— 块渲染、凭证引用、空值向后兼容。

用 `uv run pytest tests/agent/tools/taskflow -q` 运行（CI 门禁是分组独立进程运行器 `tests/run_tests_split.py`）。
