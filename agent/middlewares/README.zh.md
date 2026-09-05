# EMA Agent 中间件系统

[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-blue)]()
[![LangChain 1.3+](https://img.shields.io/badge/LangChain-1.3%2B-orange)]()

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

EMA AI Agent 的中间件层：八个 `AgentMiddleware` 组件，作用于每一次模型调用与工具调用——上下文工程、多模态输入处理、迭代预算、工具护栏、对话记录修复、心跳卡死检测、人工审批以及上下文摘要——外加一个供 worker agent 使用的输出重复防护。

> 本文档中的每一项陈述都已对照源代码核实（已安装的 `langchain 1.3.9`、`agent/core.py`、`agent/tools/subagent/spawn/core.py` 以及 `agent/middlewares/` 下的各模块）。下文出现的类名、文件名、默认值与状态键均真实存在于代码中。

---

## 目录

- [架构总览](#架构总览)
- [中间件链](#中间件链)
- [中间件参考](#中间件参考)
  - [ContextEngineHook](#contextenginehook)
  - [MultimodalProcessor](#multimodalprocessor)
  - [IterationBudget](#iterationbudget)
  - [ToolGuardrails](#toolguardrails)
  - [ToolCallNormalize](#toolcallnormalize)
  - [SubagentCompletionDrainMiddleware](#subagentcompletiondrainmiddleware)
  - [HeartbeatStaleness](#heartbeatstaleness)
  - [HumanInTheLoop](#humanintheloop)
  - [Summarization](#summarization)
  - [OutputRepetitionGuard 与 RepetitionGuardWrapper](#outputrepetitionguard-与-repetitionguardwrapper)
- [共享状态系统](#共享状态系统)
- [配置](#配置)
- [生命周期与数据流](#生命周期与数据流)
- [编写自定义中间件](#编写自定义中间件)
- [附录](#附录)

---

## 架构总览

### 什么是中间件？

中间件继承 `langchain.agents.middleware.AgentMiddleware`，在 Agent 循环的明确定义好的位置接入。系统使用四个钩子家族（均提供同步与异步两种形式）：

| 钩子家族 | 同步 | 异步 | 作用范围 |
|---|---|---|---|
| Agent 前/后 | `before_agent` / `after_agent` | `abefore_agent` / `aafter_agent` | 每个对话回合一次，围绕整个模型–工具循环 |
| 模型前/后 | `before_model` / `after_model` | `abefore_model` / `aafter_model` | 围绕每一次单独的模型请求 |
| 模型调用包装 | `wrap_model_call` | `awrap_model_call` | 拦截模型请求本身（修改消息 / 系统提示词、短路 LLM） |
| 工具调用包装 | `wrap_tool_call` | `awrap_tool_call` | 拦截每一次工具执行 |

### 钩子顺序语义

以下结论已对照已安装的 `langchain 1.3.9` 源码核实（`agents/middleware/factory.py` 与 `agents/middleware/types.py`）：

- `before_agent` 钩子按**列表顺序**执行——先注册的先运行。
- `after_agent` 钩子按**列表逆序**执行——最后注册的中间件的 `after_agent` 最先运行（它是编译图中出口节点的调用链）。
- `wrap_model_call` / `wrap_tool_call` 的组合方式是：**列表中第一个中间件为最外层**，最后一个为最内层（最贴近 LLM / 工具）。

> ⚠️ 旧版中间件框架使用 `awrap_before_agent` 风格的钩子；LangChain 1.3 没有。异步形式是直接加 `a` 前缀：`abefore_agent`、`abefore_model`、`aafter_model`、`aafter_agent`、`awrap_model_call`、`awrap_tool_call`。

### 状态持久化

中间件状态**不**存放在 LangGraph 图状态中（少数由框架管理的键除外）。跨调用状态保存在按会话隔离的运行时寄存器里：

- `state_register_mem`（`StateRegisterMeM`）——内存字典，易失（进程重启即清空）。
- `state_register_db`（`StateRegisterDB`）——SQLite 持久化（`src/data/state_register.db`），重启后仍保留。
- `timer_call_register`（`TimerCallRegister`）——后台倒计时定时器（1–60 分钟），由 `HeartbeatStaleness` 使用。

详见[共享状态系统](#共享状态系统)。

---

## 中间件链

### 主 Agent（`agent/core.py`）

```python
middleware = [
    ContextEngineHook(),
    MultimodalProcessor(),
    IterationBudget(90),
    ToolGuardrails(),
    ToolCallNormalize(),
    HeartbeatStaleness(),
    HumanInTheLoop(HITLConfig()),
    Summarization(
        need_update_system_prompt=True,
        model=auxiliary_llm,
        main_llm_context_window=main_llm_max_tokens,
        trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
        keep=("messages", 10),
    ),
]
# create_agent(model=main_llm, tools=tools, middleware=middleware, ...)
# 编译后的图再被包装：
agent = RepetitionGuardWrapper(_agent, phantom_stream_guard=True)
```

`main_llm_max_tokens` 读取自环境变量 `MAIN_LLM_MAX_TOKEN`（`models/LLMs/main_llm.py`），因此主 Agent 的摘要触发点位于主模型上下文窗口的 80 % 处（`COMPRESSION_TRIGGER_RATIO = 0.80`）。

> **注意：** `OutputRepetitionGuard` **没有**注册为主 Agent 的中间件。主 Agent 的相应行为由包装编译图的 `RepetitionGuardWrapper` 提供——见 [OutputRepetitionGuard 与 RepetitionGuardWrapper](#outputrepetitionguard-与-repetitionguardwrapper)。

### Worker / 子 Agent 流水线（`agent/tools/subagent/spawn/core.py`）

```python
middleware = [
    Summarization(
        model=auxiliary_llm,
        main_llm_context_window=main_llm_max_tokens,
        trigger=[
            ("messages", 40),
            ("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO)),
        ],
        keep=("messages", 10),
    ),
    IterationBudget(60),
    ToolGuardrails(),
    OutputRepetitionGuard(),
    ToolCallNormalize(),
    HeartbeatStaleness(),
]
# 子图以同样方式包装：
child_agent = RepetitionGuardWrapper(child_graph, phantom_stream_guard=True)
```

与主 Agent 的差异：

- 摘要触发条件改为消息数（40）**或** token 数（上下文窗口的 80 %），而非仅 token。
- 更紧的迭代预算（60 而非 90）。
- 没有 `ContextEngineHook`、`MultimodalProcessor`、`HumanInTheLoop`。
- `OutputRepetitionGuard` 在这里作为真正的中间件运行。
- 子会话结束时，spawn 代码会在 `finally` 块中从 `state_register_mem` 删除 `OutputRepetitionGuard` 的六个状态键（`SESSION_STATE_KEYS`）。

### 每回合的实际执行顺序（主 Agent）

| 阶段 | 顺序 |
|---|---|
| `before_agent`（列表顺序） | ContextEngineHook → MultimodalProcessor → IterationBudget → ToolGuardrails → ToolCallNormalize → HeartbeatStaleness → HumanInTheLoop → Summarization |
| `wrap_model_call`（最外层 → 最内层） | ContextEngineHook → MultimodalProcessor → IterationBudget → ToolGuardrails → ToolCallNormalize → HeartbeatStaleness → HumanInTheLoop → Summarization（Summarization 最贴近 LLM） |
| `after_agent`（逆序） | Summarization → HumanInTheLoop → HeartbeatStaleness → ToolCallNormalize → ToolGuardrails → IterationBudget → MultimodalProcessor → ContextEngineHook |

只有实现了某个钩子的中间件才会参与该阶段；表中展示的是如果实现的话各自所处的位置。

---

## 中间件参考

### ContextEngineHook

**模块：** `agent/middlewares/context_engine/core.py` · **类：** `ContextEngineHook(AgentMiddleware)`
**钩子：** `wrap_model_call` / `awrap_model_call`、`wrap_tool_call` / `awrap_tool_call`、`after_agent` / `aafter_agent`

列表中的第一个，因此是最外层的包装层。

**`wrap_model_call` —— 系统提示词注入**

1. 先查 `state_register_mem` 中的 `system_prompt`。
2. 回退到 `state_register_db`；若仍缺失，则通过 `workspace.prompt_builder.build_system_prompt(session_id)` 重建。
3. 通过 `request.override(system_message=...)` 注入，并把提示词缓存回 `state_register_mem`。

**`wrap_tool_call` —— 技能复盘计数**

对每一次工具调用，将 `state_register_db` 中的 `nudge_review_skill_count` 加一，除非该工具的元数据设置了 `nudge: true`（nudge/limit 工具自我豁免）。

**`after_agent` / `aafter_agent` —— 回合收尾**

1. 将 `state_register_db` 中的 `nudge_review_memory_count` 加一。
2. 若计数器达到阈值——`_NUDGE_MEMORY_THRESHOLD = 10` 回合、`_NUDGE_SKILL_THRESHOLD = 10` 次工具调用——则在 `state_register_mem` 的会话级锁 `nudge_review_memory_lock` / `nudge_review_skill_lock` 保护下启动对应的 **nudge 子 Agent**（见下）。持锁期间 `after_agent` 跳过 nudge 判定（计数器仍会递增）。
3. 将最后一个回合持久化到 MesMemory：`slice_last_turn` → `sanitize_tool_use_result_pairing` → `add_messages(session_id, messages)`（SQLite）。
4. 同步 `after_agent` 通过 `run_async` 运行子 Agent；`aafter_agent` 通过 `asyncio.gather` 并发执行持久化与 nudge。

**Nudge 子 Agent**（`context_engine/nudge.py`）：基于主 LLM 构建的独立 `create_agent` 实例，中间件为 `[_NudgeLimitTool(), ToolCallNormalize(), ToolGuardrails(), IterationBudget()]`。`_NudgeLimitTool` 会拒绝所有元数据缺少 `nudge: true` 的工具，因此 nudge Agent 只能使用记忆/技能类工具。提示词：`_MEMORY_REVIEW_PROMPT`（记忆复盘）、`_SKILL_REVIEW_PROMPT`（技能库复盘）、`_COMBINED_REVIEW_PROMPT`（两者合并）。

> 本文档的旧版本声称存在知识图谱维护（`after_turn`）和 `MemoryCache`。**当前代码中两者都不存在。** 系统提示词来自状态寄存器与 `build_system_prompt()`；中间件层没有任何知识图谱调用。

### MultimodalProcessor

**模块：** `agent/middlewares/multimodal_processor.py` · **类：** `MultimodalProcessor(AgentMiddleware)`
**钩子：** `before_agent` / `abefore_agent`、`after_agent` / `aafter_agent`

`before_agent` 在最后一条 `HumanMessage` 的内容为多模态列表时对其进行处理：

- **文本**条目直接透传（至多一条）。
- **`image_url`**：远程 `http(s)` URL 原样保留；`data:` / base64 载荷被解码并用 PIL 保存到 `src/<session_id>/mutil_temp/<时间戳><扩展名>`（扩展名通过 `_IMAGE_MAGIC` 魔数推断），同时在 `media/` 中保留一份持久副本。
- **`audio_url`**：下载到临时文件（30 秒超时）。**`audio_bytes` / `video_url` / `video_bytes`**：以同样方式解码保存（`_AUDIO_MAGIC` / `_VIDEO_MAGIC`）。
- 消息文本末尾追加 `"[Uploaded media]"` 指令块，告知模型使用 `skill_view` 工具 `image_to_text` / `speech_to_text` / `video_text_to_text` 查看文件（模型本身没有原生视觉能力）。
- 持久化路径写入 `additional_kwargs["images"]` / `["audios"]` / `["videos"]`，随后由 MesMemory 写库供历史渲染使用。
- **更早的** `HumanMessage` 中的 `image_url` 块会被剥离，避免过期的 base64 大对象滞留在上下文中。

`after_agent` 清理 `mutil_temp`：删除文件名主干不是纯数字时间戳、或超过 7 天的文件。

### IterationBudget

**模块：** `agent/middlewares/iteration_budget.py` · **类：** `IterationBudget(AgentMiddleware)`
**钩子：** `before_agent` / `abefore_agent`、`wrap_model_call` / `awrap_model_call`、`wrap_tool_call` / `awrap_tool_call`

对**一个回合内模型调用 + 工具调用总和**的硬上限。构造函数：`__init__(max_iterations: int = 50)`；主 Agent 注册 `IterationBudget(90)`，worker Agent 注册 `IterationBudget(60)`。

- `before_agent` 在 `state_register_mem` 中重置计数器：`iteration_budget = max_iterations`、`iteration_budget_used = 0`。
- `wrap_model_call` 每次模型调用消耗 1；预算耗尽时直接返回终止 `AIMessage`，**不再调用模型**。
- `wrap_tool_call` 每次工具调用消耗 1；耗尽时返回错误 `ToolMessage`（"Tool [x] skipped — iteration budget exhausted"），不再执行。

### ToolGuardrails

**模块：** `agent/middlewares/tool_guardrails.py` · **类：** `ToolGuardrails(AgentMiddleware)`
**钩子：** `before_agent` / `abefore_agent`、`wrap_tool_call` / `awrap_tool_call`

检测五种失败病理，并以四级升级 `ALLOW → WARN → BLOCK → HALT`（`GuardrailAction` 枚举）作出反应：

| 病理 | 触发条件 | WARN 阈值 | BLOCK 阈值 | hard-stop 模式 |
|---|---|---|---|---|
| 精确失败重复 | 相同工具 + 相同参数（参数 JSON `sort_keys` 后取 MD5）失败 | 2（`exact_failure_warn_after`） | 5（`exact_failure_block_after`） | 5 次时 HALT |
| 同工具失败累积 | 相同工具以**不同**参数反复失败 | 3（`same_tool_failure_warn_after`） | 8（`same_tool_failure_halt_after`） | 8 次时 HALT |
| 幂等无进展 | 元数据 `idempotent: true` 的工具返回相同的结果哈希 | 2（`no_progress_warn_after`） | 5（`no_progress_block_after`） | 5 次时 HALT |
| 乒乓 | 两个工具之间不间断的只读 A → B → A → B 往返 | 4（`ping_pong_warn_after`） | 6（`ping_pong_block_after`） | 6 次时 HALT |
| 参数翻新 | 同一幂等工具轮换不同参数变体 | 3 种变体（`arg_churn_warn_after`） | 5 种变体（`arg_churn_block_after`） | 5 种时 HALT |

- `before_agent` 重置回合级护栏状态（`state_register_mem` 中的键 `tool_guardrail_state`）——严格回合作用域，新回合从干净状态开始。
- `wrap_tool_call` 先做拦截预检（对被阻止的工具/终止状态直接返回错误 `ToolMessage`，不执行），再运行工具，然后评估结果：
  - `warn` 在 `ToolMessage` 后附加警告；
  - `block` 将工具记入 `blocked_tools`；
  - `halt` 为本回合剩余时间设置粘性终止（`halt_decision`）。
- **恢复模式**（`recovery_mode_enabled=True` 默认开启）：第一次 BLOCK 不会把回合打入死牢。回合进入恢复状态，*precheck* 路径会放行被拦的工具，让重试得到全新评估。此后每次 BLOCK 都会递增违规计数器；一旦计数超过 `recovery_max_violations`（默认 1），动作升级为 HALT——一个受管的重试窗口，而不是一堵立即竖起的墙。
- **乒乓配对**对相邻两次调用的工具名做哈希，且只在*连续两次*调用都是成功的幂等调用（两条记录都带结果哈希）时才累加。任何错误，或任何一次成功的非幂等（有副作用）调用，都会把所有已累计的配对连击清零。结果内容从不参与比较：不间断的只读往返本身就是循环信号。非幂等工具的成功同样会重置参数翻新状态。
- `ToolCallGuardrailConfig` 默认值：`warnings_enabled=True`、`hard_stop_enabled=False`、`recovery_mode_enabled=True`、`recovery_max_violations=1`——当 `hard_stop_enabled=True` 时，每个*阻止*阈值都会变成 HALT（旧的严格之墙）；`recovery_mode_enabled=False` 则恢复立即阻止的行为。

▶️ 完整文档：[docs/harness/loop-prevention/README.md](../../docs/harness/loop-prevention/README.md) · [中文](../../docs/harness/loop-prevention/README.zh.md) · [한국어](../../docs/harness/loop-prevention/README.ko.md) · [日本語](../../docs/harness/loop-prevention/README.ja.md)

### ToolCallNormalize

**模块：** `agent/middlewares/tool_call_normalize.py` · **类：** `ToolCallNormalize(AgentMiddleware)`
**钩子：** 仅 `before_model` / `abefore_model`

在上下文裁剪后修复 tool-call / tool-result 配对，防止提供方报 "Message ordering conflict" 错误。委托给 `pub_func.sanitize_tool_use_result_pairing(state["messages"])`（定义于 `pub_func/transcript_repair.py`），它会：

- 按 `tool_call_id` 对 `ToolMessage` 去重；
- 丢弃空的 `ToolMessage`；
- 为缺失的结果插入占位 `ToolMessage`（"tool result missing after context trim."）；
- 清除错误状态 `AIMessage` 上的 `invalid_tool_calls`，避免其被序列化成 OpenAI tool_calls。

钩子返回完整的消息替换：`[RemoveMessage(id=REMOVE_ALL_MESSAGES), *repaired]`。

### SubagentCompletionDrainMiddleware

**模块：** `agent/middlewares/subagent_completion_drain.py` · **类：** `SubagentCompletionDrainMiddleware(AgentMiddleware)`
**钩子：** 仅 `before_model` / `abefore_model`

在主 Agent 中注册于 `ToolCallNormalize` 之后，因此它注入的消息在注入回合不会经过 sanitize 重写。它在 `before_model` 时重建并清空（drain）当前会话的 `SteeringQueue`——父会话忙碌期间由 announce 管线排队的完成载体消息——返回 `{"messages": [carrier, ...]}`，在下一次模型调用前注入重建的完成载体 `HumanMessage`。

- 每个被取出的队列条目都会在队列的 SQLite 存储中标记为 `CONSUMED`，因此载体只会被注入一次（检查点持久化保证 HITL 恢复重放安全）。
- Fail-open：`session_id` 缺失/为空、队列为空或任何异常都会被吞掉（记日志 + 无操作）——drain 绝不会破坏父回合，队列保留以供重试。
- 注入的载体以 `origin='subagent_completion'` 持久化到 MesMemory。

### HeartbeatStaleness

**模块：** `agent/middlewares/heartbeat_staleness.py` · **类：** `HeartbeatStaleness(AgentMiddleware)`
**钩子：** `before_agent` / `abefore_agent`、`after_agent` / `aafter_agent`、`wrap_model_call` / `awrap_model_call`、`wrap_tool_call` / `awrap_tool_call`

卡死回合的看门狗。**主 Agent 与 worker Agent 都有注册**（本文档旧版本声称只在 worker 使用——那是错的）。

- `before_agent` 重置状态键，并通过 `timer_call_register.register(..., execute_now=True)` 启动后台定时器（1 分钟节奏）。
- `wrap_model_call` 将 `heartbeat_iter` 加一——但若此前的心跳检查已判定杀死回合，则先抛出 `HeartbeatTimeoutError`。`wrap_tool_call` 在工具运行期间设置 `heartbeat_tool`，返回后清除。
- 定时器回调将 `(heartbeat_iter, heartbeat_tool)` 与 `_last_heartbeat_iter` / `_last_heartbeat_tool` 比较：有进展则清零过期计数，无进展则加一。空闲状态下累计 `stale_cycles_idle = 7` 次无进展，或卡在同一工具内累计 `stale_cycles_in_tool = 20` 次，则置 `heartbeat_killed = True`——下一次模型 / 工具调用将抛出 `HeartbeatTimeoutError` 而不是继续执行。
- `after_agent` 停止定时器。
- 状态键：`heartbeat_iter`、`heartbeat_tool`、`heartbeat_stale`、`heartbeat_killed`，以及 `_last_heartbeat_iter` / `_last_heartbeat_tool`。

### HumanInTheLoop

**模块：** `agent/middlewares/humanInTheLoop/core.py` · **类：** `HumanInTheLoop(AgentMiddleware)`
**钩子：** `before_agent` / `abefore_agent`、`after_model` / `aafter_model`、`wrap_tool_call` / `awrap_tool_call`

在主 Agent 中以 `HumanInTheLoop(HITLConfig())` 注册——全部默认值，即模式 `ApprovalMode.SMART`。在每次模型响应后拦截工具调用，并在策略要求时用 LangGraph 原生 `interrupt()` 挂起图，让前端渲染审批对话框。被拒绝的调用替换为错误 `ToolMessage`（`BLOCKED_MESSAGE`）；`GraphInterrupt` 会被重新抛出，绝不吞掉。

`after_model` 中对每次工具调用的处理流水线：

1. 硬红线 / 危险命令检测（`detection.py`：`detect_hardline_command`、`detect_dangerous_command`，底层为 `HARDLINE_PATTERNS` / `DANGEROUS_PATTERNS`），经由 `ApprovalPipeline.check_command`（`approval.py`）。
2. 智能审批（`ApprovalMode.SMART`，可选 `smart_approval_llm`）——自动放行明显安全的调用。
3. `interrupt()` ——默认决策超时 60 秒。
4. 当 `write_approval_memory=True` 时，记忆工具写入经过 `WriteApprovalGate`；列入 `interrupted_tools` 的工具总是中断，决策为 `approve` / `edit` / `reject`（`edit` 会改写工具调用的参数/名称）。
5. `wrap_tool_call` 拒绝执行审批被拒或超时的调用（回合级标志在 `before_agent` 中重置）。

子门控（`gates.py` / `approval.py`）：`ApprovalPipeline`、`WriteApprovalGate`、`InterruptManager`、`MCPElicitationConsent`、`KanbanTriage`、`PairingStore`、`SlashConfirm`。状态以 `hitl:` 前缀键存放在 `state_register_mem`。

`HITLConfig` 默认值：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `mode` | `ApprovalMode.SMART` | `SMART` / `MANUAL` / `OFF` |
| `timeout` | `60` | 中断决策超时 |
| `deny_rules` | `[]` | 显式拒绝规则 |
| `yolo_mode` | `False` | 跳过所有审批 |
| `write_approval_memory` | `False` | 记忆工具写入需审批 |
| `write_approval_skills` | `False` | 技能写入需审批 |
| `clarify_timeout` | `3600` | 澄清提问超时 |
| `kanban_recurrence_limit` | `3`（`BLOCK_RECURRENCE_LIMIT`） | 触发看板分诊前的重复阻止上限 |
| `mcp_reload_confirm` | `True` | MCP 服务器重载需确认 |
| `destructive_slash_confirm` | `True` | 破坏性斜杠命令需确认 |
| `smart_approval_llm` | `None` | 用于智能自动审批的 LLM |
| `interrupted_tools` | `{}` | 总是触发 `interrupt()` 的工具 |
| `description_prefix` | `"Action requires human approval"` | 审批对话框标题前缀 |

▶️ 完整文档：[humanInTheLoop/README.md](humanInTheLoop/README.md) · [中文](humanInTheLoop/README.zh.md) · [한국어](humanInTheLoop/README.ko.md) · [日本語](humanInTheLoop/README.ja.md)

### Summarization

**模块：** `agent/middlewares/summarization.py` · **类：** `Summarization(AgentMiddleware)`
**钩子：** `before_agent` / `abefore_agent`（计数器重置）、`wrap_model_call` / `awrap_model_call`

最内层的中间件——最贴近 LLM。从零实现的 `AgentMiddleware`（**并非** LangChain 的 `SummarizationMiddleware`）：触发条件命中后，按预算制截断点压缩历史——优先非 LLM 策略，仅在文本降级安全时才使用辅助 LLM 摘要。`keep` 参数被接受但未使用；尾部保留纯预算制：`clamp(context_window × 0.25, 2 000, 15 000)` 个 token（`PRESERVE_RATIO` / `MIN_PRESERVE_TOKENS` / `MAX_PRESERVE_TOKENS`）。

- **生命周期与路由**：中间件现覆盖五个触发点（T1–T5）——T1 预检（`before_agent` / `abefore_agent`）、T2 调用前派发（`wrap_model_call` / `awrap_model_call`）、T3 响应后复检（真实上报 token）、T4（413 Payload Too Large）/ T5（上下文溢出）错误恢复环——每次触发都运行四路溢出路由决策（truncate / compact / both / pass），并委托给 `pub_func/message/overflow_router.py`、`pub_func/message/tool_result_ttl.py`、`pub_func/message/llm_error_classifier.py`。状态存于会话级 `summarization_*` 键（共 14 个，每回合重置 10 个）。完整文档见下方链接。
- **触发语义**：单个子句是 `("messages", N)` 或 `("tokens", N)`；子句列表之间是 **OR**——任一子句命中即开始压缩。主 Agent：`[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))]`；worker：`[("messages", 40), ("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))]`。`COMPRESSION_TRIGGER_RATIO = 0.80`。
- **截断点安全：** `_determine_cutoff` 选定截断点，随后 `_adjust_for_orphan_pairs` 向前回退，直到没有任何 `ToolMessage` 与其 `AIMessage` 工具调用被拆开；当最后一个用户回合占估算 token 的 ≥ 50 % 时（`LAST_TURN_RATIO_THRESHOLD = 0.5`），会改为对最后一个回合本身做压缩（`self._compress_last_turn` 标志），而不是把它摘要掉。
- **防抖动：** 每个**会话**至多 `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5` 次压缩（而非每回合）；连续 `INEFFECTIVE_THRESHOLD = 2` 次无效压缩后（有效 = 消息数减少，或 token 缩减 ≥ `MIN_EFFECTIVENESS_PCT = 0.05`），LLM 步骤被禁用（`summarization_skip_llm`），仅运行非 LLM 策略。计数器以会话级 `summarization_*` 键存于 `state_register_mem`（压缩次数、无效连击、上次 token、上次策略、跳过标志、恢复状态等）。
- **截断：** 已有的摘要消息（以 `additional_kwargs["lc_source"] == "summarization"` 识别）超过 `SUMMARY_TOTAL_MAX_CHARS = 16 000` 字符时被重新截断，保留头部 30 % / 尾部 30 %（`CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO`），并加入省略标记。
- **输出：** 替换后的消息是 `HumanMessage` / `AIMessage` **成对出现**——一条中性的 `"What did we do so far?"`，后跟携带 `additional_kwargs={"lc_source": "summarization"}` 的 `AIMessage`——因此模型不会看到两条连续同角色消息，也无需事后配对修复。
- `need_update_system_prompt=True`（仅主 Agent）：压缩完成后重建系统提示词——重载记忆库后调用 `build_system_prompt()`——并以 `system_prompt` 键写回两个状态寄存器。

▶️ 完整文档：[docs/harness/summarization/README.md](../../docs/harness/summarization/README.md) · [中文](../../docs/harness/summarization/README.zh.md) · [한국어](../../docs/harness/summarization/README.ko.md) · [日本語](../../docs/harness/summarization/README.ja.md)

> 预算中间件的类默认值 `max_iterations` 为 50；*实际注册*值是 90（主）与 60（worker）。本文档旧版本声称预算为 10——那是错的。

### OutputRepetitionGuard 与 RepetitionGuardWrapper

**模块：** `agent/middlewares/output_repetition_guard.py` · **类：** `OutputRepetitionGuard(AgentMiddleware)`
**钩子：** `before_agent` / `abefore_agent`、`wrap_model_call` / `awrap_model_call`

事后式的输出重复检测器，带 `WARN → HALT` 升级。从 `agent.middlewares.output_repetition_guard` 导出（**没有**被 `agent/middlewares/__init__.py` 再导出），且**仅在 worker 流水线中注册**。

主 Agent 的同类检测由 **`RepetitionGuardWrapper`**（`agent/stream_repetition_guard_wrapper.py`）完成：它包装编译后的图，在流式层面拦截（外加 `ainvoke` 事后兜底），复用相同的状态键与默认值。两处注册均传入 `phantom_stream_guard=True`。

**检测层**

- **跨调用重复** —— 对可见输出的最后 `_TAIL_CHARS = 500` 个字符取 MD5，与滚动历史（`_MAX_HISTORY = 30`）比较。连续 `warn_after = 2` 次相同输出 → WARN（`AIMessage` 提醒）；`max_identical_outputs = 3` 次 → HALT，返回终止 `AIMessage` 并置粘性终止标志。
- **单次输出内部重复**：
  - 句子/行重复占比 > `internal_repeat_ratio = 0.6`（且分段数 ≥ `internal_min_lines = 6`）；
  - 出现 ≥ `char_run_min = 8` 个连续相同的非空白字符；
  - 2–10 字符的短语重复 ≥ 5 次。

  内部警告按标签每会话只触发一次。
- 少于 `_MIN_CONTENT_LENGTH = 20` 字符的内容跳过；含工具调用的模型响应整体跳过（工具循环结束后会再次检查）。
- **推理内容单独跟踪**（`additional_kwargs` 中的 `reasoning_content` / `reasoning` / `reasoning_text`，以及内联的 `<think>` / `<thinking>` / `<reasoning>` 块——会被提取并从可见内容中剥离）。

**流式辅助函数** `check_stream_repetition(session_id, accumulated_text)` —— 共享的 `_STREAM_GUARD` 单例，被 `server/service/messages.py::async_generate` 用于在检测到重复时中途截断流式响应；它共享同一组状态键与相同的内部警告去重门。

**Worker 清理：** 子会话结束时，`SESSION_STATE_KEYS`（六个键）会从 `state_register_mem` 中删除。

---

## 共享状态系统

所有跨调用的中间件状态都按会话隔离，存放在两个寄存器加一个定时器注册表中：

| 寄存器 | 底层存储 | 说明 |
|---|---|---|
| `state_register_mem`（`StateRegisterMeM`） | 内存字典 | 易失；`_initialized` 守卫保证进程启动时只重置一次 |
| `state_register_db`（`StateRegisterDB`） | SQLite（`src/data/state_register.db`） | 重启后仍保留；不支持 `clear_session`（返回 `False`）；提供 `get_all_session_ids` |
| `timer_call_register`（`TimerCallRegister`） | asyncio 定时器 | `register(session_id, name, callback, args, minutes 1–60, execute_now=False)` |

通用接口（`runtime/state_register.py`）：`set_state`、`get_state`、`get_all_states`、`delete_state`、`clear_session`、`has_session`、`has_key`、`update_states`。

### 命名空间约定

| 键 | 归属 | 寄存器 |
|---|---|---|
| `system_prompt` | ContextEngineHook / Summarization | mem + db |
| `nudge_review_memory_count`、`nudge_review_skill_count` | ContextEngineHook | db |
| `nudge_review_memory_lock`、`nudge_review_skill_lock` | ContextEngineHook | mem |
| `iteration_budget`、`iteration_budget_used` | IterationBudget | mem |
| `tool_guardrail_state` | ToolGuardrails | mem |
| `summarization_*` 键（压缩计数器、无效连击、上次 token/策略、跳过 LLM 标志、恢复状态、上次用户提问） | Summarization | mem |
| `heartbeat_iter`、`heartbeat_tool`、`heartbeat_stale`、`heartbeat_killed`、`_last_heartbeat_iter`、`_last_heartbeat_tool` | HeartbeatStaleness | mem |
| OutputRepetitionGuard 的键（`SESSION_STATE_KEYS`，六个） | OutputRepetitionGuard / RepetitionGuardWrapper | mem |
| `hitl:` 前缀键（`_STATE_PREFIX = "hitl"`） | HumanInTheLoop | mem |

---

## 配置

### 环境变量与配置项

| 配置项 | 位置 | 作用 |
|---|---|---|
| `MAIN_LLM_MAX_TOKEN` | `.env` → `models/LLMs/main_llm.py` | 主 Agent 摘要触发点 = 该值的 80 %；同时作为 `main_llm_context_window` 传入 |

> **相关但独立：** 各工具的超时是写死的模块常量——`WEB_SEARCH_TIMEOUT = 15`（`agent/tools/web_search.py`）、`TERMINAL_TIMEOUT = 30`（`agent/tools/terminal.py`）、`PYTHON_REPL_TIMEOUT = 30`（`agent/tools/python_repl.py`；超时会杀死子进程）。`.env.example` 中的 `TOOL_CALL_TIMEOUT_MINUTES = 5` **没有任何代码消费**——它不是生效的配置项。`config/num.py` 的常量（`ARCHIVE_THRESHOLD`、`MEMORY_THRESHOLD`、`COMPRESS_RATIO`）也没有被中间件层使用。

### 构建示例

```python
from langchain.agents import create_agent
from agent.middlewares import (
    ContextEngineHook, MultimodalProcessor, IterationBudget, ToolGuardrails,
    ToolCallNormalize, HeartbeatStaleness, HumanInTheLoop, HITLConfig, Summarization,
)

agent = create_agent(
    model=main_llm,
    tools=tools,
    middleware=[
        ContextEngineHook(),          # 系统提示词 + nudge + 持久化
        MultimodalProcessor(),        # 多模态输入规范化
        IterationBudget(90),          # 回合级调用预算
        ToolGuardrails(),             # 失败病理检测
        ToolCallNormalize(),          # tool_use/tool_result 修复
        HeartbeatStaleness(),         # 卡死回合看门狗
        HumanInTheLoop(HITLConfig()), # 审批门控
        Summarization(                # 上下文压缩（最内层）
            need_update_system_prompt=True,
            model=auxiliary_llm,
            main_llm_context_window=main_llm_max_tokens,
            trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
            keep=("messages", 10),
        ),
    ],
)
```

### 各中间件参数

| 中间件 | 参数 | 默认值 | 实际注册值 |
|---|---|---|---|
| `IterationBudget` | `max_iterations` | `50` | `90`（主）/ `60`（worker） |
| `Summarization` | `need_update_system_prompt` | `False` | `True`（主） |
| `Summarization` | `model` | 必填 | `auxiliary_llm` |
| `Summarization` | `main_llm_context_window` | 必填 | `main_llm_max_tokens` |
| `Summarization` | `trigger` | 必填 | 见[中间件链](#中间件链) |
| `Summarization` | `keep` | 必填 | `("messages", 10)`（接受但未使用） |
| `ToolGuardrails` | `config: ToolCallGuardrailConfig` | 见上文默认值 | 默认值 |
| `HumanInTheLoop` | `config: HITLConfig` | 见上文默认值 | 默认值 |
| `HeartbeatStaleness` | （默认） | 间隔 1 分钟，空闲 7 / 工具内 20 | 默认值 |
| `OutputRepetitionGuard` | （默认） | 3 / 2 / 0.6 / 6 / 8 | 默认值 |

---

## 生命周期与数据流

### 单回合详解

```
用户回合到达
│
├─ before_agent（列表顺序）
│   ContextEngineHook → MultimodalProcessor → IterationBudget → ToolGuardrails
│   → ToolCallNormalize → HeartbeatStaleness → HumanInTheLoop → Summarization
│   · ContextEngineHook   此处无操作（持久化在 after_agent 进行）
│   · MultimodalProcessor  规范化最后一条 HumanMessage，剥离旧 image_url 块
│   · IterationBudget  重置预算计数器
│   · ToolGuardrails  重置回合级护栏状态
│   · HeartbeatStaleness  重置状态键 + 启动 1 分钟心跳定时器
│   · HumanInTheLoop  重置回合级中断标志
│   · Summarization  重置压缩计数器
│
├─ 循环：模型调用
│   ├─ before_model
│   │   · ToolCallNormalize  sanitize_tool_use_result_pairing + RemoveMessage 重写
│   ├─ wrap_model_call（最外层 → 最内层）
│   │   · ContextEngineHook  注入系统提示词（request.override）
│   │   · IterationBudget  消耗 1；耗尽时返回终止 AIMessage
│   │   · HeartbeatStaleness  已杀死则抛 HeartbeatTimeoutError；否则 heartbeat_iter += 1
│   │   · Summarization  视情况压缩历史（非 LLM 策略 + 辅助 LLM），防抖计数
│   ├─ LLM 响应
│   └─ after_model
│       · HumanInTheLoop  策略检查；必要时 interrupt()；阻止 → 错误 ToolMessage
│
├─ 循环：工具调用（每次调用）
│   └─ wrap_tool_call
│       · IterationBudget  消耗 1；耗尽时返回错误 ToolMessage
│       · ToolGuardrails  预检 block/halt → 执行 → 评估 → warn/block/halt
│       · ContextEngineHook  技能复盘计数（除非工具元数据 nudge: true）
│       · HeartbeatStaleness  已杀死则抛出；设置 heartbeat_tool，返回后清除
│       · HumanInTheLoop  拒绝审批被拒/超时的调用
│
└─ after_agent（逆序）
    Summarization → HumanInTheLoop → HeartbeatStaleness → ToolCallNormalize
    → ToolGuardrails → IterationBudget → MultimodalProcessor → ContextEngineHook
    · HeartbeatStaleness  停止心跳定时器
    · MultimodalProcessor  清理 mutil_temp（> 7 天 / 非数字文件名）
    · ContextEngineHook  记忆复盘计数 → 视情况启动 nudge 子 Agent（持锁）
                        → 将最后回合持久化到 MesMemory（slice → sanitize → add_messages）
```

---

## 编写自定义中间件

继承 `AgentMiddleware`，只覆盖需要的钩子（签名来自已安装的 `langchain 1.3.9`——状态钩子接收 `(state, runtime)`，包装钩子接收 `(request, handler)`）：

```python
from langchain.agents.middleware import AgentMiddleware


class MyMiddleware(AgentMiddleware):
    """每回合前后各运行一次。"""

    def before_agent(self, state, runtime):
        # 返回状态更新字典，或 None
        return None

    def after_agent(self, state, runtime):
        return None

    def wrap_model_call(self, request, handler):
        # 检查/修改 `request`，然后委托给 `handler(request)`
        return handler(request)

    def wrap_tool_call(self, request, handler):
        return handler(request)
```

异步变体遵循 `a` 前缀约定：`abefore_agent`、`aafter_agent`、`awrap_model_call`、`awrap_tool_call` 等。包装钩子要保持轻量、少副作用——它们在**每一次**模型/工具调用时都会运行；且在本代码库中，第一个注册的中间件是最外层包装。

---

## 附录

### 文件布局

```
agent/middlewares/
├── __init__.py                  # 公开导出
├── context_engine/              # ContextEngineHook + nudge 子 Agent
│   ├── __init__.py              # 仅导出 ContextEngineHook
│   ├── core.py                  # ContextEngineHook
│   └── nudge.py                 # nudge 提示词 + 子 Agent 构建器
├── heartbeat_staleness.py       # HeartbeatStaleness
├── humanInTheLoop/              # HumanInTheLoop + HITLConfig（有自己的 README）
│   ├── __init__.py              # 导出 HumanInTheLoop、HITLConfig
│   ├── types.py                 # 枚举 + 配置数据类（_STATE_PREFIX = "hitl"）
│   ├── detection.py             # 硬红线 / 危险命令模式
│   ├── approval.py              # ApprovalPipeline
│   ├── gates.py                 # WriteApprovalGate、InterruptManager、MCPElicitationConsent、
│   │                            # KanbanTriage、PairingStore、SlashConfirm
│   └── core.py                  # HumanInTheLoop
├── iteration_budget.py          # IterationBudget
├── multimodal_processor.py      # MultimodalProcessor
├── output_repetition_guard.py   # OutputRepetitionGuard（不在下方再导出之列）
├── summarization.py             # Summarization
├── tool_call_normalize.py       # ToolCallNormalize
├── tool_guardrails.py           # ToolGuardrails
└── README.md                    # 本文件（+ .zh / .ja / .ko 变体）

agent/stream_repetition_guard_wrapper.py  # RepetitionGuardWrapper（位于本包之外）
```

### 导出（`__init__.py`）

```python
from agent.middlewares import (
    Summarization,
    ToolGuardrails,
    IterationBudget,
    ContextEngineHook,
    ToolCallNormalize,
    HeartbeatStaleness,
    MultimodalProcessor,
    HumanInTheLoop,
    HITLConfig,
)
# OutputRepetitionGuard 不在此处再导出——请从
# agent.middlewares.output_repetition_guard 导入。
```
