# EMA Agent 中间件系统

[![Python 3.13+](https://img.shields.io/badge/Python-3.13%2B-blue)]()
[![LangChain 1.3+](https://img.shields.io/badge/LangChain-1.3%2B-orange)]()

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

EMA AI Agent 的中间件层：作用于每一次模型调用与工具调用的 `AgentMiddleware` 组件——上下文工程、多模态输入处理、迭代预算、工具护栏、对话记录修复、心跳卡死检测、人工审批、模型边界与工具返回双时机落库（`MessagePersistenceMiddleware`）、上下文摘要，以及带模型回退的分类式 LLM 错误重试（`LLMRetryMiddleware`）——外加输出重复防护与流式图包装器（`RepetitionGuardWrapper`、`ContextLimitGuardWrapper`）。

> 本文档中的每一项陈述都已对照源代码核实（已安装的 `langchain 1.3.9`、`agent/core.py`、`agent/tools/subagent/spawn/core.py` 以及 `agent/middlewares/` 下的各模块）。下文出现的类名、文件名、默认值与状态键均真实存在于代码中。

---

## 目录

- [架构总览](#架构总览)
- [中间件链](#中间件链)
- [中间件参考](#中间件参考)
  - [system_prompt_injection](#system_prompt_injection)
  - [MultimodalProcessor](#multimodalprocessor)
  - [IterationBudget](#iterationbudget)
  - [ToolGuardrails](#toolguardrails)
  - [ToolCallNormalize](#toolcallnormalize)
  - [PathGuard](#pathguard)
  - [SubagentCompletionDrainMiddleware](#subagentcompletiondrainmiddleware)
  - [TaskIntentMiddleware](#taskintentmiddleware)
  - [TodoContinuationEnforcer](#todocontinuationenforcer)
  - [HeartbeatStaleness](#heartbeatstaleness)
  - [HumanInTheLoop](#humanintheloop)
  - [MessagePersistenceMiddleware](#messagepersistencemiddleware)
  - [ContextEvictionMiddleware](#contextevictionmiddleware)
  - [LLMRetryMiddleware](#llmretrymiddleware)
  - [Summarization](#summarization)
  - [MaxTokensBoostMiddleware](#maxtokensboostmiddleware)
  - [OutputRepetitionGuard 与 RepetitionGuardWrapper](#outputrepetitionguard-与-repetitionguardwrapper)
  - [ContextLimitGuardWrapper](#contextlimitguardwrapper)
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

以下结论已对照已安装的 `langchain 1.3.9` 源码核实（`langchain/agents/factory.py` 与 `langchain/agents/middleware/types.py`）：

- `before_agent` 钩子按**列表顺序**执行——先注册的先运行。
- `after_agent` 钩子按**列表逆序**执行——最后注册的中间件的 `after_agent` 最先运行（它是编译图中出口节点的调用链）。
- `after_model` 钩子每个中间件编译成一个图节点，并按**列表逆序**串联——`model` → `after_model[last]` → … → `after_model[first]`。因此，列表里最后一个实现该钩子的中间件，就是模型产出后第一个运行的钩子。
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
    # after_agent 钩子按列表逆序执行：最先注册的 enforcer 在回合结束时
    # 最后运行，从而观察到真正结束的回合。
    TodoContinuationEnforcer(),
    system_prompt_injection,  # @dynamic_prompt：系统提示词注入
    MultimodalProcessor(),
    IterationBudget(ITERATION_BUDGET["main_agent_max_iterations"]),
    ToolGuardrails(),
    ContextEvictionMiddleware(),
    ToolCallNormalize(),
    PathGuard(),
    SubagentCompletionDrainMiddleware(),
    TaskIntentMiddleware(),
    OutputRepetitionGuard(),
    MaxTokensBoostMiddleware(),
    HeartbeatStaleness(),
    HumanInTheLoop(HITLConfig()),
    # after_model 节点按注册逆序执行：紧跟 HITL 注册，即为模型产出后
    # 第一个运行的钩子，AI 消息先落库，之后 HITL 才会剥离被拒调用 / 中断。
    MessagePersistenceMiddleware(),
    LLMRetryMiddleware(fallback_chain=fallback_chain),
    Summarization(
        need_update_system_prompt=True,
        model=auxiliary_llm,
        main_llm_context_window=main_llm_max_tokens,
        trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
        keep=("messages", 10),
    ),
]
# create_agent(model=main_llm, tools=tools, middleware=middleware, ...)
# 编译后的图再交给 apply_graph_wrappers() 包装（由内到外）：
# RepetitionGuardWrapper，然后是 ContextLimitGuardWrapper。
```

`main_llm_max_tokens` 读取自环境变量 `MAIN_LLM_MAX_TOKEN`（`models/LLMs/main_llm.py`），因此主 Agent 的摘要触发点位于主模型上下文窗口的 80 % 处（`COMPRESSION_TRIGGER_RATIO = 0.80`）。

> **注意：** `OutputRepetitionGuard` **已**注册为主 Agent 的中间件（逐调用拦截），**并且**编译后的图还额外被 `RepetitionGuardWrapper` 包装以进行流式层面的检测——见 [OutputRepetitionGuard 与 RepetitionGuardWrapper](#outputrepetitionguard-与-repetitionguardwrapper)。

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
    MaxTokensBoostMiddleware(),
    ToolCallNormalize(),
    HeartbeatStaleness(),
]
# 子图以同样方式包装：
child_agent = RepetitionGuardWrapper(child_graph, phantom_stream_guard=True)
```

与主 Agent 的差异：

- 摘要触发条件改为消息数（40）**或** token 数（上下文窗口的 80 %），而非仅 token。
- 更紧的迭代预算（60 而非 90）。
- 没有 `system_prompt_injection`（`@dynamic_prompt`）、`MultimodalProcessor`、`HumanInTheLoop`、`LLMRetryMiddleware`（子 Agent 没有分类式重试/回退循环）、`PathGuard`、`TaskIntentMiddleware`、`TodoContinuationEnforcer`。
- 没有 `MessagePersistenceMiddleware`：子会话不属于客户端可见的 MesMemory 历史 —— 其对话只存在于检查点，仅父会话可见的完成载体以 `origin='subagent_completion'` 落库。
- 没有 `ContextEvictionMiddleware`：子会话保留完整的工具结果（不写驱逐文件、不做 read_file 切片），超长人类消息也不打标、不截断视图。
- `OutputRepetitionGuard` 在这里作为真正的中间件运行。
- `MaxTokensBoostMiddleware` 走非流式路径：子会话通过
  `ainvoke` 运行，因此子会话 id 永远不会置上 `is_stream_turn` 标志。
- 子会话结束时，spawn 代码会在 `finally` 块中从 `state_register_mem` 删除 `OutputRepetitionGuard` 的六个状态键（`SESSION_STATE_KEYS`）。

### 每回合的实际执行顺序（主 Agent）

| 阶段 | 顺序 |
|---|---|
| `before_agent`（列表顺序） | MultimodalProcessor → IterationBudget → ToolGuardrails → OutputRepetitionGuard → HeartbeatStaleness → HumanInTheLoop → Summarization |
| `before_model`（列表顺序） | ContextEvictionMiddleware（P1-9：给末条超长 HumanMessage 打标；`before_agent` 链已先运行，媒体提示已并入文本） → ToolCallNormalize → SubagentCompletionDrainMiddleware → TaskIntentMiddleware（两个注入器都排在净化重写之后，因此其消息在注入回合不会被剥离） |
| `wrap_model_call`（最外层 → 最内层） | system_prompt_injection → MultimodalProcessor → IterationBudget → ContextEvictionMiddleware → OutputRepetitionGuard → MaxTokensBoostMiddleware → HeartbeatStaleness → LLMRetryMiddleware → Summarization（Summarization 最贴近 LLM；LLMRetry 从外部包住 Summarization 的 T4/T5 恢复环，并位于 MaxTokensBoost 内层，因此只看到真正的截断） |
| `after_model`（逆序） | MessagePersistenceMiddleware → HumanInTheLoop（落库先跑：它是列表里最后一个实现该钩子的中间件，且自身 fail-open，因此 HITL 的拒绝改写与 `GraphInterrupt` 都无法跳过落库） |
| `wrap_tool_call`（最外层 → 最内层） | IterationBudget → ToolGuardrails → ContextEvictionMiddleware → PathGuard → HeartbeatStaleness → HumanInTheLoop → MessagePersistenceMiddleware（最内层、最贴近工具：工具返回即落库其 `ToolMessage`；HITL 的中断/拒绝短路会跳过它，这些拒绝在下一次模型边界落库。驱逐位于落库外层：先落库原文，只有预览继续进入 state） |
| `after_agent`（逆序） | HeartbeatStaleness → MultimodalProcessor → TodoContinuationEnforcer（HeartbeatStaleness 是最后一个注册 `after_agent` 的实现者，因此最先运行；最先注册的 enforcer 最后运行，看到已结束的回合） |

表中只列出在该阶段实现了对应钩子的中间件。

---

## 中间件参考

### system_prompt_injection

**模块：** `agent/middlewares/system_prompt/core.py` · **中间件：** `system_prompt_injection`（由 LangChain `@dynamic_prompt` 创建的 `AgentMiddleware` 实例）
**钩子：** `wrap_model_call` / `awrap_model_call`

列表中的第二个，紧跟 `TodoContinuationEnforcer`（后者不实现任何模型调用包装），因此是最外层的 **wrap** 层。装饰器生成的类同时注册 `wrap_model_call` 与 `awrap_model_call`（被装饰函数是同步的，异步包装器同样调用它）。

**系统提示词注入**

1. 用 `require_session_id` 解析 `session_id`——缺失/空白会抛 `RuntimeError("Not pass session_id")`。
2. 先查 `state_register_mem` 中的 `system_prompt`。
3. 回退到 `state_register_db`；若仍缺失，则通过 `workspace.prompt_builder.build_system_prompt(session_id)` 重建，并**双写 db + mem**。
4. same-content skip：`@dynamic_prompt` 生成的包装器*无条件*调用 `request.override(system_message=...)`。当请求上已有的 `SystemMessage` 内容相同时，被装饰函数返回**同一个 message 对象**，override 重新施加的字节完全一致——模型可见前缀保持逐字节一致（前缀缓存友好）；仅当内容真正变化（或首轮 `request.system_message is None`）才返回新的 `SystemMessage`。

> `system_prompt` 这个 mem 键是与压缩管线的契约：`Summarization` 读取它做 token 估算（`_estimate_system_prompt_tokens`），并在压缩后重写（mem + db）——因此缓存未命中时必须双写。

**持久化已不再属于压缩管线。** `MessagePersistenceMiddleware` 在每个模型边界把新消息落库到 MesMemory（见下文小节）；记忆复盘 / 计划提取两个 nudge 仍由 `Summarization` 从 compact 接缝调度。`system_prompt_injection` 不重写任何生命周期钩子（`before_agent` / `after_agent` / `before_model` / `after_model`）；它只负责系统提示词包装。

> 本文档的旧版本声称存在知识图谱维护（`after_turn`）和 `MemoryCache`。**当前代码中两者都不存在。** 系统提示词来自状态寄存器与 `build_system_prompt()`；中间件层没有任何知识图谱调用。

### MultimodalProcessor

**模块：** `agent/middlewares/media_pipeline/core.py` · **类：** `MultimodalProcessor(AgentMiddleware)`
**钩子：** `before_agent` / `abefore_agent`、`wrap_model_call` / `awrap_model_call`、`after_agent` / `aafter_agent`

`before_agent` 在最后一条 `HumanMessage` 的内容为多模态列表时对其进行处理：

- **文本**条目直接透传（至多一条）。
- **`image_url`**：远程 `http(s)` URL 原样保留；`data:` / base64 载荷被解码并用 PIL 保存到 `src/<session_id>/mutil_temp/<时间戳><扩展名>`（扩展名通过 `_IMAGE_MAGIC` 魔数推断），同时在 `media/` 中保留一份持久副本。
- **`audio_url`**：下载到临时文件（30 秒超时）。**`audio_bytes` / `video_url` / `video_bytes`**：以同样方式解码保存（`_AUDIO_MAGIC` / `_VIDEO_MAGIC`）。
- **体积上限**：写盘之前先校验体积，超过 `max_media_bytes`（20 MiB，与 DeepAgents CLI 硬限一致）的载荷被跳过——不写盘、绝不记录为路径、以 warning 记录实际字节数，提示写入 `MediaPaths.skipped`，由处理器作为 `[Uploaded media] ... was skipped` 一行追加到消息文本块。远程 URL 优先按 `Content-Length` 判定，缺失时按带上限的流式读取判定，因此错误响应头也无法导致超限写盘。恰好等于上限的载荷允许通过；0 字节 / 非法载荷沿用既有失败路径。
- `main_llm_native_multimodal` 配置决定路径：`"true"` 为主模型保留原始媒体块，`"false"` 始终走技能路径，`"auto"` 则按媒体类型（vision / audio / video）查询进程级能力缓存（键为 `"{provider}/{model_name}"`）逐类决策。
- `"auto"` 下：**全部**在途类型均缓存为 `"unsupported"` 时整条消息走技能路径；**混合**消息（部分类型 supported、部分 unsupported）保留原生块，由每请求擦洗层只替换不支持的那些块；未探测（`"auto"`）的类型保留其块并写入本回合的原生尝试标志（`_multimodal_trying_native` / `_multimodal_native_model`）。`"[Uploaded media]"` 指令块仅在技能路径下追加，告知模型使用 `skill_view` 工具 `image_to_text` / `speech_to_text` / `video_text_to_text` 查看文件。
- 当模型拒绝被分类为 `multimodal_not_supported` 时，`LLMRetryMiddleware` 会把消息中实际出现的媒体类型写为 `"unsupported"`，并把请求改写为技能路径；同进程内的后续会话与回合因此不再尝试原生。拒绝会记在**实际服务的**模型上：当该调用已被重绑到回退候选时，`LLMRetryMiddleware` 会在写缓存前把 `_multimodal_native_model` 改写为该候选（见其小节）。
- **静默降级检测**（`main_llm_silent_degradation_detection`，默认开启）：当原生尝试**成功**、但回复自述无法感知媒体或要求用户描述附件时，`LLMRetryMiddleware` 会把请求中出现的媒体类型针对实际服务的模型缓存为 `"unsupported"`，后续回合不再做注定失败的原生探测。检测器（`media_pipeline/degradation.py::detect_media_blindness`）是对 EN/ZH/JA/KO 的高精度正则：只有媒体词出现在失明/描述短语附近时才判定。原先"回复从未提及媒体内容"的字面启发式被刻意弃用——有能力的模型可以完全不使用媒体关键词就描述图片，而误判会让较慢的技能路径永久生效。
- 持久化路径写入 `additional_kwargs["images"]` / `["audios"]` / `["videos"]`，随后由 MesMemory 写库供历史渲染使用。
- **更早的** `HumanMessage` 中的 `image_url` 块会被剥离，避免过期的 base64 大对象滞留在上下文中；但仅在确实存在此类块时才执行剥离（廉价前置检查会跳过无可剥离内容的消息），且仅当剥离后的文本非空时才写回。

`wrap_model_call` / `awrap_model_call` 在**每次模型请求**上运行（仅 `"auto"` 模式），擦洗请求副本：族被缓存为 `"unsupported"` 的媒体块被替换为文本占位符，其中写明被剥离的媒体、其落盘路径与对应技能（`image_to_text` / `speech_to_text` / `video_text_to_text`）；supported 与未探测的块原样保留。改写使用 `request.override(messages=...)`——state、checkpointer 与 MesMemory 均不被触碰——无需擦洗时原样返回原请求对象。能力查询使用环境主模型 key（`get_model_key()`）：本层包在 `LLMRetryMiddleware` 之外，无法感知在链路更内层重绑的粘性回退候选；该候选的拒绝仍由 `multimodal_not_supported` → 技能路径改写覆盖——写缓存前它会把实际服务的候选写进 `_multimodal_native_model`。

`after_agent` 清理 `mutil_temp`：删除文件名主干不是纯数字时间戳、或超过 7 天的文件。

### IterationBudget

**模块：** `agent/middlewares/iteration_budget/core.py` · **类：** `IterationBudget(AgentMiddleware)`
**钩子：** `before_agent` / `abefore_agent`、`wrap_model_call` / `awrap_model_call`、`wrap_tool_call` / `awrap_tool_call`

对**一个回合内模型调用 + 工具调用总和**的硬上限。构造函数：`__init__(max_iterations: int = 50)`；主 Agent 注册 `IterationBudget(90)`，worker Agent 注册 `IterationBudget(60)`。

- `before_agent` 在 `state_register_mem` 中重置计数器：`iteration_budget = max_iterations`、`iteration_budget_used = 0`。
- `wrap_model_call` 每次模型调用消耗 1；预算耗尽时直接返回终止 `AIMessage`，**不再调用模型**。
- `wrap_tool_call` 每次工具调用消耗 1；耗尽时返回错误 `ToolMessage`（"Tool [x] skipped — iteration budget exhausted"），不再执行。

### ToolGuardrails

**模块：** `agent/middlewares/tool_guardrails/core.py` · **类：** `ToolGuardrails(AgentMiddleware)`
**钩子：** `before_agent` / `abefore_agent`、`wrap_tool_call` / `awrap_tool_call`

检测五种失败病理，并以四级升级 `ALLOW → WARN → BLOCK → HALT`（`GuardrailAction` 枚举）作出反应：

| 病理 | 触发条件 | WARN 阈值 | BLOCK 阈值 | hard-stop 模式 |
|---|---|---|---|---|
| 精确失败重复 | 相同工具 + 相同参数（参数 JSON `sort_keys` 后取 MD5）失败 | 2（`exact_failure_warn_after`） | 5（`exact_failure_block_after`） | 5 次时 HALT |
| 同工具失败累积 | 相同工具以**不同**参数反复失败 | 3（`same_tool_failure_warn_after`） | 8（`same_tool_failure_halt_after`） | 8 次时 HALT |
| 幂等无进展 | 同一幂等工具（元数据 `idempotent: true`）返回本回合中它已产出过的结果哈希 | 2（`no_progress_warn_after`） | 5（`no_progress_block_after`） | 5 次时 HALT |
| 乒乓 | 两个工具之间不间断的 A → B → A → B 往返，且相邻两次调用均无进展（stagnant） | 4（`ping_pong_warn_after`） | 6（`ping_pong_block_after`） | 6 次时 HALT |
| 参数翻新 | 同一幂等工具轮换参数变体，但结果保持不变（无进展） | 3 种变体（`arg_churn_warn_after`） | 5 种变体（`arg_churn_block_after`） | 5 种时 HALT |

- `before_agent` 重置回合级护栏状态（`state_register_mem` 中的键 `tool_guardrail_state`）——严格回合作用域，新回合从干净状态开始。
- `wrap_tool_call` 先做拦截预检（对被阻止的工具/终止状态直接返回错误 `ToolMessage`，不执行），再运行工具，然后评估结果：
  - `warn` 在 `ToolMessage` 后附加警告；
  - `block` 将工具记入 `blocked_tools`；
  - `halt` 为本回合剩余时间设置粘性终止（`halt_decision`）。
- **恢复模式**（`recovery_mode_enabled=True` 默认开启）：第一次 BLOCK 不会把回合打入死牢。回合进入恢复状态，*precheck* 路径会放行被拦的工具，让重试得到全新评估。此后每次 BLOCK 都会递增违规计数器；一旦计数超过 `recovery_max_violations`（默认 1），动作升级为 HALT——一个受管的重试窗口，而不是一堵立即竖起的墙。
- **无进展即停滞（stagnation，`is_stagnant`）**：一次成功的幂等调用只有在**同一工具**本回合中已产出过完全相同的 `result_hash` 时才算无进展；结果发生变化即为进展。**乒乓配对**对相邻两次调用的工具名做哈希，且只在*连续两次*调用都无进展时才累加。任何错误、结果发生变化（非无进展），或任何一次成功的非幂等（有副作用）调用，都会把所有已累计的配对连击清零。幂等工具返回变化结果同样不再计入参数翻新；非幂等工具的成功则会完全清空参数翻新状态。
- `ToolCallGuardrailConfig` 默认值：`warnings_enabled=True`、`hard_stop_enabled=False`、`recovery_mode_enabled=True`、`recovery_max_violations=1`——当 `hard_stop_enabled=True` 时，每个*阻止*阈值都会变成 HALT（旧的严格之墙）；`recovery_mode_enabled=False` 则恢复立即阻止的行为。

▶️ 完整文档：[docs/loop-prevention/README.md](../../docs/loop-prevention/README.md) · [中文](../../docs/loop-prevention/README.zh.md) · [한국어](../../docs/loop-prevention/README.ko.md) · [日本語](../../docs/loop-prevention/README.ja.md)

### ToolCallNormalize

**模块：** `agent/middlewares/tool_call_normalize/core.py` · **类：** `ToolCallNormalize(AgentMiddleware)`
**钩子：** 仅 `before_model` / `abefore_model`

在上下文裁剪后修复 tool-call / tool-result 配对，防止提供方报 "Message ordering conflict" 错误。委托给 `pub.func.sanitize_tool_use_result_pairing(state["messages"])`（定义于 `pub/func/transcript_repair.py`），它会：

- 按 `tool_call_id` 对 `ToolMessage` 去重；
- 丢弃空的 `ToolMessage`；
- 为缺失的结果插入占位 `ToolMessage`（"tool result missing after context trim."）；
- 清除错误状态 `AIMessage` 上的 `invalid_tool_calls`，避免其被序列化成 OpenAI tool_calls。

无变化时钩子返回 `None`——不写状态、不重建消息，模型可见前缀原样不动；仅在实际修复后才返回完整的消息替换：`[RemoveMessage(id=REMOVE_ALL_MESSAGES), *repaired]`。注意：前缀缓存的判据是**发给模型的序列化内容**，而非 Python 对象身份；跳过无变化重建可消除无意义的 checkpointer 状态写入，并彻底排除重建路径带来的内容漂移。

### PathGuard

**模块：** `agent/middlewares/path_guard/core.py` · **类：** `PathGuard(AgentMiddleware)`
**钩子：** 仅 `wrap_tool_call` / `awrap_tool_call`

为各工具自己的 `resolve_project_path()` / `resolve_external_path()` 模式提供纵深防御：忘记做路径检查的工具仍无法被诱导读取穿越路径或硬拒绝路径。在主 Agent 中注册在 `ToolCallNormalize` 之后；由于列表顺序即 wrap 钩子的外层顺序，它运行在 `ToolGuardrails` **之内**（`IterationBudget` → `ToolGuardrails` → `ContextEvictionMiddleware` → `PathGuard` → 工具），拒绝会作为普通错误 `ToolMessage` 交给 ToolGuardrails 评估，与其他工具失败一视同仁。worker 链不注册：子 Agent 的工具保有自己的门禁，且子代理的外部路径本就硬拒绝。

筛选刻意保守：

- 只检查参数名 `file_path` / `path` / `directory` / `dir` 的字符串值；形如 `scheme://` 的 URL 跳过，非路径语义不会被误读；
- 拒绝 `..` 穿越分量（先做 URL 解码与反斜杠规范化——复用 `has_traversal_component`）；
- 能被 `resolve_project_path()` 接受的路径原样放行；
- 解析到 `ROOT_DIR` 之外的值**除非**命中硬拒绝下限（YOLO 排除列表 / `/etc/passwd`、`/etc/shadow`、`/etc/sudoers`），否则放行；
- 其余外部路径交给工具自身的 `resolve_external_path()` HITL 流程——中间件从不批准、不改写参数、不触发中断，因为工具在执行时还会再跑同一道门（在这里拦截等于做两次决定）。

拒绝时中间件返回结构化错误 `ToolMessage`（`status="error"`，保留原 `tool_call_id` / 工具名），不执行工具。外部路径的细节见 [docs/sandbox/isolation/README.zh.md §5](../../docs/sandbox/isolation/README.zh.md#5-外部文件路径门禁文件工具)。

### SubagentCompletionDrainMiddleware

**模块：** `agent/middlewares/subagent_completion_drain/core.py` · **类：** `SubagentCompletionDrainMiddleware(AgentMiddleware)`
**钩子：** 仅 `before_model` / `abefore_model`

在主 Agent 中注册于 `ToolCallNormalize` 之后，因此它注入的消息在注入回合不会经过 sanitize 重写。它在 `before_model` 时重建并清空（drain）当前会话的 `SteeringQueue`——父会话忙碌期间由 announce 管线排队的完成载体消息——返回 `{"messages": [carrier, ...]}`，在下一次模型调用前注入重建的完成载体 `HumanMessage`。

- 每个被取出的队列条目都会在队列的 SQLite 存储中标记为 `CONSUMED`，因此载体只会被注入一次（检查点持久化保证 HITL 恢复重放安全）。
- Fail-open：`session_id` 缺失/为空、队列为空或任何异常都会被吞掉（记日志 + 无操作）——drain 绝不会破坏父回合，队列保留以供重试。
- 注入的载体在它被注入的那次模型调用的 `after_model` 边界，以 `origin='subagent_completion'` 写入 MesMemory（`MessagePersistenceMiddleware`）；在该边界之前它只存在于检查点中，messages 表内不可见。

### TaskIntentMiddleware

**模块：** `agent/middlewares/task_intent/core.py` · **类：** `TaskIntentMiddleware(AgentMiddleware)`
**钩子：** `before_model` / `abefore_model`（异步钩子是生产路径；仅当事件循环未运行时同步孪生实现才委托给它）

在主 Agent 中紧跟 `SubagentCompletionDrainMiddleware` 注册，因此它注入的消息在注入回合不会经过净化重写。同一个钩子融合了两种行为：

- **武装（Arming）：** 在没有活跃计划时，第一条看起来像工作请求的用户回合注入完整编排器引导提示；之后的合格回合只注入简短提醒。武装账本（`_armed_sessions`）是进程级的；`rearm_after_compact(session_id)` 在成功压缩后清除条目（由 `Summarization` 调用），因此 compact 后会再次注入完整提示。候选识别使用关键词 / 疑问 / 闲聊模式（`_TASK_KEYWORDS`、`_QUESTION_PATTERNS`、`_CHAT_PATTERNS`）。
- **计划活跃引导：** 当 boulder 文件（`config.path.resolve_boulder_path`）中存在活跃/暂停的工作、其计划文件存在且包含复选框时，改为追加计划活跃提醒并跳过武装——计划活跃引导优先。

注入只发生在一个回合的第一次模型调用（最后一条非指令 `HumanMessage` 必须是末条消息）。被消化的完成载体（`metadata.internal` + `provenance == "subagent_completion"`）、`[SYSTEM DIRECTIVE` / `<sherry-ulw-execute>` 指令以及 `metadata.internal` 消息都不会触发引导，因此注入的指令不会重新武装该中间件。Fail-open：任何内部异常都会记录日志并返回 `None`。

### TodoContinuationEnforcer

**模块：** `agent/middlewares/todo_continuation/core.py` · **类：** `TodoContinuationEnforcer(AgentMiddleware)`
**钩子：** 仅 `aafter_agent`（异步回合结束钩子）

在主 Agent 列表中注册在**第一位**，因此其 `after_agent` **最后**运行——`after_agent` 钩子按列表逆序执行，enforcer 必须观察真正结束的回合。当会话 todo 列表仍有 `pending` / `in_progress` 项时，它通过服务器侧的自动回合钩子（`runtime.hooks.MAYBE_TRIGGER_AUTO_TURN`，调用时解析；钩子未注册时降级为 no-op，会话保持可重试）以 fire-and-forget 方式注入续作提示。

- 中止类回合错误（用户取消 / 超时，`stagnation_tracker.is_abort_error`）绝不续作；空列表或全部完成的列表会重置停滞追踪器。
- 停滞处理（`agent/tools/todolist/stagnation_tracker.py`）：列表在多次尝试后仍未变化则进入恢复模式并注入 `_RECOVERY_PROMPT`；`is_in_cooldown` 限制重复注入；只有成功投递后才通过 `mark_injected` 记录触发。
- Fail-open：任何异常都会记录日志，回合正常结束。

### HeartbeatStaleness

**模块：** `agent/middlewares/heartbeat_staleness/core.py` · **类：** `HeartbeatStaleness(AgentMiddleware)`
**钩子：** `before_agent` / `abefore_agent`、`after_agent` / `aafter_agent`、`wrap_model_call` / `awrap_model_call`、`wrap_tool_call` / `awrap_tool_call`

卡死回合的看门狗。**主 Agent 与 worker Agent 都有注册**（本文档旧版本声称只在 worker 使用——那是错的）。

- `before_agent` 重置状态键，并通过 `timer_call_register.register(..., execute_now=True)` 启动后台定时器（1 分钟节奏）。
- `wrap_model_call` 将 `heartbeat_iter` 加一——但若此前的心跳检查已判定杀死回合，则先抛出 `HeartbeatTimeoutError`。`wrap_tool_call` 在工具运行期间设置 `heartbeat_tool`，返回后清除。
- `skip_heartbeat` 旁路：元数据设置了 `skip_heartbeat: true` 的工具（基于 interrupt 的工具会把图挂起等待人工输入，其 after-tool 钩子在恢复前不会运行）会设置 `heartbeat_skip` 而非 `heartbeat_tool`；该标志置位期间，定时器回调跳过进展检查——等待期间未变化的 `(iter, tool)` 对不算停滞。标志在 after-tool 钩子中清除，并在 `before_agent` 中重置。
- 定时器回调将 `(heartbeat_iter, heartbeat_tool)` 与 `_last_heartbeat_iter` / `_last_heartbeat_tool` 比较：有进展则清零过期计数，无进展则加一。空闲状态下累计 `stale_cycles_idle = 7` 次无进展，或卡在同一工具内累计 `stale_cycles_in_tool = 20` 次，则置 `heartbeat_killed = True`——下一次模型 / 工具调用将抛出 `HeartbeatTimeoutError` 而不是继续执行。
- `after_agent` 停止定时器。
- 状态键：`heartbeat_iter`、`heartbeat_tool`、`heartbeat_stale`、`heartbeat_killed`、`heartbeat_skip`，以及 `_last_heartbeat_iter` / `_last_heartbeat_tool`。

### HumanInTheLoop

**模块：** `agent/middlewares/humanInTheLoop/core.py` · **类：** `HumanInTheLoop(AgentMiddleware)`
**钩子：** `before_agent` / `abefore_agent`、`after_model` / `aafter_model`、`wrap_tool_call` / `awrap_tool_call`

在主 Agent 中以 `HumanInTheLoop(HITLConfig())` 注册——全部默认值，即模式 `ApprovalMode.SMART`。在每次模型响应后拦截工具调用，并在策略要求时用 LangGraph 原生 `interrupt()` 挂起图，让前端渲染审批对话框。被拒绝的调用替换为错误 `ToolMessage`（`BLOCKED_MESSAGE`）；`GraphInterrupt` 会被重新抛出，绝不吞掉。

`after_model` 中对每次工具调用的处理流水线：

1. 硬红线 / 危险命令检测（`detection.py`：`detect_hardline_command`、`detect_dangerous_command`，底层为 `HARDLINE_PATTERNS` / `DANGEROUS_PATTERNS`），经由 `ApprovalPipeline.check_command`（`approval.py`）。
2. 智能审批（`ApprovalMode.SMART`，可选 `smart_approval_llm`）——自动放行明显安全的调用。
3. `interrupt()` ——默认决策超时 60 秒。
4. 当 `write_approval_memory=True` 时，记忆工具写入经过 `WriteApprovalGate`；列入 `interrupted_tools` 的工具总是中断，决策为 `approve` / `edit` / `reject`（`edit` 会改写工具调用的参数/名称）。另外，外部路径网关会发起自己的中断，决策集为 `approve` / `approve_dir` / `yolo` / `reject`（详见 [docs/sandbox/isolation/README.zh.md](../../docs/sandbox/isolation/README.zh.md#5-外部文件路径门禁文件工具)）。
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

工具调用的审批决策还会通过 `ToolApprovalStore`（`approval_store.py`）持久化到 `SRC_DIR/data/approvals.json`：按操作员隔离的 JSON 存储，以字节修订 CAS 更新，因此已批准的 `interrupted_tools` 调用在重启后不会再次询问。当范围内没有操作员（或该轮次为无人的系统注入）时，需要审批的门控会直接自动拒绝而不再中断。

▶️ 完整文档：[humanInTheLoop/README.md](humanInTheLoop/README.md) · [中文](humanInTheLoop/README.zh.md) · [한국어](humanInTheLoop/README.ko.md) · [日本語](humanInTheLoop/README.ja.md)

### MessagePersistenceMiddleware

**模块：** `agent/middlewares/message_persistence/core.py` · **类：** `MessagePersistenceMiddleware(AgentMiddleware)`
**钩子：** `after_model` / `aafter_model`、`wrap_tool_call` / `awrap_tool_call`

在主 Agent 中**紧跟 `HumanInTheLoop` 注册**；由于 `after_model` 节点按注册逆序串联，它是 `model` 之后**第一个执行**的钩子——AI 消息先落库，HITL 之后才会剥离被拒工具调用或抛 `GraphInterrupt`，任何其它钩子的异常也无法跳过落库。在工具调用包装链（列表第一个为最外层）中它位于**最内层**，因此能看到真实的工具结果，而 HITL 的短路会绕过它。（worker 流水线不注册它。）

**延迟——各类消息何时进入 `messages` 表：**

| 消息 | 落库时机 |
|---|---|
| human | 当轮首个模型边界 |
| AI（含其 `tool_calls`） | 模型产出后的那个模型边界 |
| 工具结果 | **工具 handler 返回的瞬间**（`wrap_tool_call` / `awrap_tool_call`），不再等下一次模型调用 |
| HITL 拒绝（`HumanInTheLoop` 短路产生的 `status="error"` ToolMessage） | 下一个模型边界——HITL 从外层包住本中间件，其短路不会调用内层 |

**工具返回落库：** wrap 钩子先执行 `handler(request)`，再从响应中取出 `ToolMessage`——响应可能是裸 `ToolMessage`、混合 `ToolMessage` / `Command` 的列表、或 `Command.update["messages"]` 携带消息（`_iter_tool_messages` 全部覆盖）——走同一批次管线落库后**原样**返回响应。不含 `ToolMessage` 的响应（纯导航 `Command`）不写任何行。

共享批次管线（两个钩子相同）：

1. 用 `require_session_id` 解析 `session_id`；缺失/空白（或非 dict 的工具调用 state）时静默跳过，绝不抛出。
2. 收集候选：`_is_persistable` 只保留 `human` / `ai` / `tool`，跳过带进程内 `_db_persisted` 标记的消息与 `lc_source == "summarization"` 的压缩产物。
3. 过滤持久水位：`filter_persisted_message_ids` 剔除查询键已登记在 `persisted_message_ids` 的候选。查询同时检查 **LangGraph 消息 `id`**（跨检查点序列化稳定）与 `sha1:` 内容指纹——工具结果在返回时落库还没有 id（图 reducer 之后才分配），因此后续边界或重启重放靠指纹命中，不会重复落库。
4. 补全批次：`_reconcile_denials_for_persistence` 把 HITL 拒绝的工具调用重挂到前一条 `AIMessage`（拒绝保持配对），`_dedup_tool_results` 丢弃空与重复 id 的工具结果。
5. `await add_messages(...)`（异步路径）/ `add_messages_sync(...)`（同步路径）写入；随后 `mark_message_ids_persisted` 把交给写入器的每个候选（含被去重的副本，防止再次浮现）登记为墓碑。
6. debug 日志记录写入条数与来源（`message persistence: wrote N messages at model boundary|tool return for <session>`）。

**两条路径共用同一水位，写入幂等。** 工具返回时写入的结果在后续每个边界都会被再次扫到，边界写入的 AI 消息在下一边界也会被再次扫到；图状态会累积，但水位保证每条消息只有一行——跨进程重启同样成立（消息 id 与内容指纹都能跨检查点序列化存活）。写入失败只记日志、**不**登记墓碑，下一边界会重试该批次（fail-open——落库绝不弄坏回合或工具结果）。

> 压缩路径不再做任何持久化：`compaction_persistence.py` 模块与 `_persist_discarded_messages_sync` / `_apersist_discarded_messages` 调用点均已删除。一次 compact 只做压缩并调度压缩时 nudge（见 Summarization 小节）。

### ContextEvictionMiddleware

**模块：** `agent/middlewares/context_eviction/core.py` · **类：** `ContextEvictionMiddleware(AgentMiddleware)`
**钩子：** `wrap_tool_call` / `awrap_tool_call`（P0-2/P2-4）与 `before_model` / `abefore_model` + `wrap_model_call` / `awrap_model_call`（P1-9）

在主 Agent 中注册于 **`ToolGuardrails` 之后**，因此在 wrap 链中位于 `PathGuard` / `HumanInTheLoop` / `MessagePersistenceMiddleware` 的**外层**（先注册者最外层）。`MessagePersistenceMiddleware` 保持最内层，由此形成工具结果的关键分工：内层在工具返回瞬间把**原文**写入 MesMemory，随后本层才换上预览。进入 state（以及 checkpointer、下一次模型调用）的始终只有预览，大内容从不进入前缀。子 Agent 流水线不注册本中间件：子会话保留完整工具结果。

同一中间件还持有**人类消息**路径（P1-9），其三是三态切分与工具侧相反；见下文[人类消息驱逐](#人类消息驱逐p1-9)。

**工具结果的两条缩减路径**

| 结果 | 处理 |
|---|---|
| 普通工具，文本 > `evict_threshold_chars`（20 000 字符） | 全文写入 `SESSIONS_DIR/<session_id>/evicted/<tool_call_id>_<md5[:8]>.txt`，内容替换为 head/tail 预览 |
| `read_file`（P2-4） | 文件已在磁盘——内容截为前 4 000 字符 + 恢复提示，**不写文件** |
| `write_file` / `patch_file` / `search_files` / `list_files` / `memory` / `skill_view` / `skill_list` | 永不驱逐（`excluded_tools`）；`read_file` 也在该集合中，但走上面的切片路径 |

**预览格式**（`pub/func/message/eviction.py::build_preview`，`preview_head_lines=5`、`preview_tail_lines=5`）：

```text
[evicted to: <path>]
--- head (5 lines) ---
<前 5 行>
...
--- tail (5 lines) ---
<后 5 行>
[full content: N chars, evicted at <ts>]

Use read_file(file_path='<path>', offset=0, limit=100) to read the full content in chunks.]
```

（`offset=0` 会被 `read_file` 的 `max(1, …)` 收敛为 1，指针始终有效。）替换由 `model_copy` 生成，消息 `id`、`status`、`name`、`additional_kwargs` 全部保留；多模态非文本块原样保留，只替换文本部分。

**三处存储**

| 位置 | 内容 |
|---|---|
| graph state / checkpointer / 下一次模型调用 | 仅预览 |
| MesMemory（`messages` 表） | 完整原文（由内层在工具返回时写入） |
| `SESSIONS_DIR/<session_id>/evicted/` | 逐字节一致的副本（`load_evicted()` / `read_file` 可取回） |

**水位安全。** `model_copy` 同时携带内层落库写入的进程内 `_db_persisted` 标记，下一次模型边界会跳过预览；当原文落库成功时，还会为替换消息的水位键写入墓碑——覆盖"重启后标记丢失且预览指纹与原文不再一致"的场景。两种情况都不会为同一逻辑消息写入第二行；若原文落库失败则不写墓碑，边界会以预览内容重试。

**路径安全与生命周期。** `session_id` 需通过单段安全校验（`config/path.py::is_safe_session_segment`）；空 / `.` / `..` / 含分隔符的 id 直接跳过且不落盘。`clear_session()` 会对整个 `SESSIONS_DIR/<session_id>/` 目录 rmtree，驱逐文件随会话一并删除。已驱逐的消息不会二次驱逐（幂等标记检查）。

**与压缩期 read_file 切片的互补关系**（`pub/func/message/target_truncation.py::_truncate_read_file_content`）：本中间件覆盖工具执行时，压缩路径覆盖上下文压力时（head 30 % + tail 30 %，`max_tool_output_chars = 2 000`，并给出解析得出的 1-based 续读 offset）。压缩再次切分已切片载荷时无法解析被截断的 JSON，会确定性地回退到"从头重读"提示；执行期切片本身也是幂等的。两条提示不会冲突，是同一消息的分阶段缩减。

#### 人类消息驱逐（P1-9）

用户可能粘贴超大纯文本（日志、文档、长对话导出、代码），而 `MultimodalProcessor` 只治理媒体附件、不治理这类文本。DeepAgents 的 `FilesystemMiddleware` 对此有专门机制（`human_message_token_limit_before_evict`）；本中间件移植该机制并采用 Sherry 特有的三态切分。

**触发（`before_model` / `abefore_model`）。** 门控：`human_evict_enabled` 且**最后一条**消息是 `HumanMessage`、未携带 `lc_evicted_to`、且提取文本长度**大于** `human_evict_threshold_chars`（200 000 字符 ≈ DeepAgents 默认 50 000 token × 4 字符/token）。只检查最后一条，历史用户消息永不复查。由于 `before_agent` 链（MultimodalProcessor）总在模型循环之前运行，打标时看到的已是并入媒体提示后的最终文本；本中间件的 `before_model` 节点按列表顺序先于 `ToolCallNormalize` / `SubagentCompletionDrainMiddleware`。

**打标 + 落盘。** 全文写入 `SESSIONS_DIR/<session_id>/evicted/human-<msg_id|时间戳>.md`（`evict_human_message`，`pub/func/message/eviction.py`），随后钩子返回部分状态更新 `{"messages": [tagged]}`，其中 `tagged = msg.model_copy(update={"additional_kwargs": {..., "lc_evicted_to": str(path)}})` —— **content 与 id 都不变**，标准 `add_messages` reducer 按 id 原地替换。不用 `REMOVE_ALL_MESSAGES` 哨兵、不重写全列表、state 侧不破坏前缀缓存。`session_id` 不安全（空 / `.` / `..` / 含分隔符）时直接跳过且不落盘。先写文件后打标，写失败不会留下悬空指针；单行超长导致预览不小于原文时同样跳过（与工具侧同一守卫）。

**模型视图（`wrap_model_call` / `awrap_model_call`）。** 凡带 `lc_evicted_to` 的 `HumanMessage` 都**只在本次请求中**被替换为基于 state 原文构建的预览（`build_human_preview`）：驱逐路径 + head/tail 各 5 行 + `read_file(file_path=..., offset=0, limit=100)` 续读提示；`_build_evicted_content` 保留非文本块（图片 / 音频 / 视频）原样，只替换文本块。请求经 `request.override(messages=...)` 重建——与 Summarization 同一 idiom——state 永不被改写。文件缺失（会话目录被清、磁盘问题）时，中间件用 state 原文**自愈**重写；目标不是本会话自己的 `evicted/` 目录时拒绝写入。

**三处存储——与工具结果相反的切分**

| 位置 | 工具结果（P0-2） | 人类消息（P1-9） |
|---|---|---|
| graph state / checkpointer | 仅预览 | **完整原文** + `lc_evicted_to` 标记 |
| MesMemory（`messages` 表） | 完整原文 | **完整原文**（标记不会过滤落库） |
| 下一次模型调用（请求视图） | 预览 | 预览（路径 + `read_file` 提示） |
| `SESSIONS_DIR/<session_id>/evicted/` | 逐字节副本 | 逐字节副本 |

差异原因：人类消息在回合的首个 `after_model` 边界才落库，state 必须保留全文，MesMemory 才能归档全文（否则 `message_search` 与压缩只能看到预览）；工具结果则由内层持久化在工具返回时即时落库，state 可以只留预览。人类消息 id 不变，持久化水位不受影响、不会写第二行。HITL 与工具配对不与 `HumanMessage` 交互；P1-2 溢出尾部裁剪只 stub `ToolMessage`，`_filter_summary_messages` 只丢弃 `lc_source='summarization'` 产物——两者都不会销毁标记或指针。

**配置**（`config/features/agent_side/tool_result_eviction.py`）：`enabled=True`、`evict_threshold_chars=20_000`、`preview_head_lines=5`、`preview_tail_lines=5`、`eviction_subdir="evicted"`、`excluded_tools`（上述 8 个）、`human_evict_enabled=True`、`human_evict_threshold_chars=200_000`、`human_preview_head_lines=5`、`human_preview_tail_lines=5`。

### LLMRetryMiddleware

**模块：** `agent/middlewares/llm_retry/core.py` · **类：** `LLMRetryMiddleware(AgentMiddleware)`（另有 `LLMRetryConfig`、`FallbackCandidate`、`ContentFilterError`）
**钩子：** 仅 `wrap_model_call` / `awrap_model_call`

在主 Agent 中注册于 **`HumanInTheLoop` 与 `Summarization` 之间**：相对 `MaxTokensBoostMiddleware` 为内层（只看到 boost 重呼之后仍未解决的真正截断），相对 Summarization 为外层（重试环从外部包住 T4/T5 溢出恢复环）。worker 流水线中**不**注册。当状态中没有 `session_id` 时，中间件直接透传。

每次 handler 调用都经过基于 `pub/func/message/llm_error_classifier.py` 的"分类 → 处置"循环——`FailoverReason` 枚举（19 种原因）、`ClassifiedError` 判定（`retryable` / `should_compress` / `should_fallback` 标志）、8 步优先级管线 `classify_api_error`——并通过 `pub/func/retry_utils.py::jittered_backoff` 退避。

**按 `FailoverReason` 的重试语义**

| 类别 | 原因 | 处置 |
|---|---|---|
| 抖动退避重试（`retryable=True`） | `auth`、`rate_limit`、`overloaded`、`server_error`、`timeout`、`image_too_large`、`invalid_response`、`unknown` | 至多重试 `max_retries` 次；延迟 = `base_delay × 2^(attempt−1)`，封顶 `max_delay`，±`jitter`，钳制在 `[0.1, max_delay]` |
| 压缩委托（`should_compress=True`） | `context_overflow`、`payload_too_large` | 立即重新抛出——溢出错误归 Summarization 的 T4/T5 恢复环管 |
| 多模态技能回退（`retryable=True`） | `multimodal_not_supported` | 仅在 auto 模式的原生尝试中：把消息中实际出现的媒体类型写为 `unsupported`，将请求改写为技能路径，重置重试预算后重呼；其他情况重新抛出 |
| 回退（`should_fallback=True`，不可重试） | `auth_permanent`、`billing`、`upstream_rate_limit`、`ssl_cert_verification`、`model_not_found`、`provider_policy_blocked`、`content_policy_blocked` | 切换到下一个回退候选；链耗尽 → 重新抛出 |
| 硬失败 | `format_error` | 重新抛出（不重试、不回退） |

**过期连击熔断器（跨回合）：** 每一次被分类为 timeout 的失败——以及每一次 timeout 分类的部分流桩（partial-stub）重试——都会把会话级 `llm_stale_streak`（存于 `state_register_mem`）加一；任何一次成功的 handler 调用都会把它清零。当连击达到 `stale_giveup_threshold` 时，下一次模型调用会在**调用 LLM 之前**抛出 `RuntimeError("Provider unresponsive — aborting to avoid indefinite stall.")`，跨回合终结提供方持续无响应的僵局。

**模型回退链：** `FallbackCandidate(provider, model_name, model)` 条目由 `models/LLMs/main_llm.py::build_fallback_chain()` 从环境变量 `FALLBACK_LLM_{i}_{PROVIDER,NAME,API_KEY,API_BASE}` 构建（i = 1…，遇到第一个缺失的 `NAME` 即停止；`PROVIDER` 默认 `openai`；客户端无法构造的候选会带警告跳过）。1 起始的激活索引按会话粘性存于 `llm_fallback_index`：每次模型调用开始时，请求都会通过 `request.override(model=...)` 重新绑定到已激活的候选；遇到可回退分类的失败（或内容过滤标志）时激活下一个候选。未配置任何 `FALLBACK_LLM_*`（默认情况）时，中间件就是一个普通的有界重试环。

**原生尝试的归属跟随实际服务的模型：** `MultimodalProcessor` 在 auto 模式原生尝试开始时把环境主模型 key 写入 `_multimodal_native_model`。每当请求被重绑到回退候选（调用开始时的粘性绑定或激活时），`_refresh_native_model_key` 都会把该键改写为 `{candidate.provider}/{candidate.model_name}`——因此媒体拒绝（或静默降级命中）会被缓存到实际服务该调用的模型上，较差候选不会污染环境主模型的缓存条目。不在进行中的原生尝试内时该方法不写任何状态。

**静默降级评估：** 处理函数成功返回后，中间件对结果运行 `_evaluate_silent_degradation`。它只在 auto 模式原生尝试期间生效；无论是否命中，本回合标志都会被清除（过期标志绝不能为同一回合后续调用授权回退）。当 `main_llm_silent_degradation_detection` 开启、且回复自述无法感知媒体（`media_pipeline/degradation.py::detect_media_blindness`）时，请求中出现的每个媒体族都会针对实际服务的模型缓存为 `"unsupported"`——后续回合不再做原生探测，而不是在拿不到媒体的情况下静默作答。

**内容过滤标志消费：** 流式层（`server/service/stream_dispatch.py`）会设置 `llm_content_filter_blocked`（显式 `finish_reason == "content_filter"`）或 `llm_content_filter_terminated`（流中安全拦截）。中间件在每次 handler 调用之后——无论成功还是已分类异常——检查这两个标志，清除它们，然后要么重新绑定到回退模型，要么在无候选可用时抛出 `ContentFilterError("Model declined to respond (safety refusal).")`。`content_policy_blocked` 永不重试。

**部分流桩消费：** 流被网络中断切断后，流式层会设置 `llm_partial_stream_stub` 与 `llm_partial_stream_cause`（保留的 `FailoverReason` 值，默认 `timeout`）。中间件在成功的 handler 调用之后消费该标志：被切断的结果被丢弃，handler 在退避后以全新尝试重呼一次——绝不加大 max_tokens 提额。流式回合（`is_stream_turn` 标志）下，重呼前先剥离 `request.config["callbacks"]` 并在 `finally` 中恢复（MaxTokensBoost 的 strip → call → restore 契约），避免已流出的 token 重复输出。timeout 分类的原因会累加过期连击。重试预算耗尽时，中间件优雅降级并返回当前（部分）结果。

**状态键（全部存于 `state_register_mem`）：** `llm_stale_streak`、`llm_fallback_index`（本中间件所有）；`llm_content_filter_blocked`、`llm_content_filter_terminated`、`llm_partial_stream_stub`、`llm_partial_stream_cause`（流式层写入，本中间件消费）；`_multimodal_trying_native`、`_multimodal_native_model`（`MultimodalProcessor` 写入，本中间件消费以实现技能回退）。

### Summarization

**模块：** `agent/middlewares/summarization/core.py` · **类：** `Summarization(AgentMiddleware)`
**钩子：** `before_agent` / `abefore_agent`（计数器重置）、`wrap_model_call` / `awrap_model_call`

最内层的中间件——最贴近 LLM。从零实现的 `AgentMiddleware`（**并非** LangChain 的 `SummarizationMiddleware`）：触发条件命中后，按预算制截断点压缩历史——优先非 LLM 策略，仅在文本降级安全时才使用辅助 LLM 摘要。`keep` 参数被接受但未使用；尾部保留纯预算制：`clamp(context_window × 0.25, 2 000, 15 000)` 个 token（`PRESERVE_RATIO` / `MIN_PRESERVE_TOKENS` / `MAX_PRESERVE_TOKENS`）。

- **生命周期与路由**：中间件现覆盖五个触发点（T1–T5）——T1 预检（`before_agent` / `abefore_agent`）、T2 调用前派发（`wrap_model_call` / `awrap_model_call`）、T3 响应后复检（真实上报 token）、T4（413 Payload Too Large）/ T5（上下文溢出）错误恢复环——每次触发都运行四路溢出路由决策（truncate / compact / both / pass），并委托给 `pub/func/message/overflow_router.py`、`pub/func/message/tool_result_ttl.py`、`pub/func/message/tool_args_truncate.py`（工具调用参数截断）、`pub/func/message/llm_error_classifier.py`。状态存于会话级 `summarization_*` 键（共 13 个，每回合重置 11 个）。完整文档见下方链接。
- **触发语义**：单个子句是 `("messages", N)` 或 `("tokens", N)`；子句列表之间是 **OR**——任一子句命中即开始压缩。主 Agent：`[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))]`；worker：`[("messages", 40), ("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))]`。`COMPRESSION_TRIGGER_RATIO = 0.80`。
- **截断点安全：** `_determine_cutoff` 选定截断点，随后 `_adjust_for_orphan_pairs` 向前回退，直到没有任何 `ToolMessage` 与其 `AIMessage` 工具调用被拆开；当最后一个用户回合占估算 token 的 ≥ 50 % 时（`LAST_TURN_RATIO_THRESHOLD = 0.5`），会改为对最后一个回合本身做压缩（`self._compress_last_turn` 标志），而不是把它摘要掉。
- **防抖动：** 每个**会话**至多 `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5` 次压缩（而非每回合）；连续 `INEFFECTIVE_THRESHOLD = 2` 次无效压缩后（有效 = 消息数减少，或 token 缩减 ≥ `MIN_EFFECTIVENESS_PCT = 0.05`），LLM 步骤被禁用（`summarization_skip_llm`），仅运行非 LLM 策略。计数器以会话级 `summarization_*` 键存于 `state_register_mem`（压缩次数、无效连击、上次 token、上次策略、跳过标志、恢复状态等）。
- **压缩时媒体归档：** 被丢弃的前缀在摘要前会先经 `offload_inline_media`（`summarization/media_offload.py`），把其中的内联媒体（`data:` URL、裸 `base64` 字段、原始字节）改写为 `SESSIONS_DIR/<session_id>/media/{sha256[:16]}{ext}`（按内容哈希去重；该子目录由 `clear_session` 整体删除），并把每个块替换为 `[evicted to: <path>]` 文本指针——`_collect_evicted_refs` 扫描的正是这个标记，因此路径会进入 `SummaryDoc.evicted_refs` 并在摘要链上存活。只有被摘要的区间会被改写；保留的尾窗媒体块不受影响。一切失败都 fail-open：无法解码 / 写入的块变为 `<media error="failed_to_offload" />`。
- **截断：** 已有的摘要消息（以 `additional_kwargs["lc_source"] == "summarization"` 识别）超过 `SUMMARY_TOTAL_MAX_CHARS = 16 000` 字符时被重新截断，保留头部 30 % / 尾部 30 %（`CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO`），并加入省略标记。
- **输出：** 替换后的消息是 `HumanMessage` / `AIMessage` **成对出现**——一条中性的 `"What did we do so far?"`，后跟携带 `additional_kwargs={"lc_source": "summarization"}` 的 `AIMessage`——因此模型不会看到两条连续同角色消息，也无需事后配对修复。
- **结构化摘要：** 辅助调用经 `with_structured_output(SummaryDoc, method="json_mode")`（`summarization/summary_doc.py`）；Markdown 由文档代码渲染，cap 后的文档本体存于 AIMessage 的 `additional_kwargs["summary_doc"]`（链式再压缩以 `<prior-summary-json>` 回喂）。解析失败退化为 `json_repair`，再退化为旧 free-form 路径；条目上限是数组切片（`completed[-5:]`、`key_decisions[-5:]`、`active_plan_notes[-20:]`、`evicted_refs[-20:]`），不再解析 Markdown。
- `need_update_system_prompt=True`（仅主 Agent）：压缩完成后重建系统提示词——重载记忆库后调用 `build_system_prompt()`——并以 `system_prompt` 键写回两个状态寄存器。两条送达路径（压缩后直送、防抖闸门路径）在请求已带相同内容的 `SystemMessage` 时会跳过注入——不 override、不新建 `SystemMessage`——从而保持模型可见前缀逐字节一致。
- **不再持久化：** 压缩路径不向 MesMemory 写任何内容。消息持久化在每个模型边界由 `MessagePersistenceMiddleware` 完成；原先 `compaction_persistence.py` 的被丢弃前缀落库及其 `_persist_discarded_messages_sync` / `_apersist_discarded_messages` 调用点均已删除。
- **压缩时 nudge：** `schedule_compression_nudges`（`summarization/nudges.py`）每次压缩都派发记忆复盘；计划提取在同一时点用 `_detect_todo_all_complete` 评估。两者都以 fire-and-forget 任务在 NUDGE 车道上运行；nudge 锁被持有时压缩完全跳过派发。after-agent 钩子不再派发它们。

**Nudge 子 Agent**（`summarization/nudges.py`，由压缩管线调度）：基于主 LLM 构建的独立 `create_agent` 实例，中间件为 `[_NudgeLimitTool(), ToolCallNormalize(), ToolGuardrails(), IterationBudget(90)]`。`_NudgeLimitTool` 会拒绝所有元数据缺少 `nudge: true` 的工具，因此 nudge Agent 只能使用 nudge 阶段白名单内的工具。共有两个提示词：

- `_MEMORY_REVIEW_PROMPT`（记忆复盘）：周期性运行，通过记忆工具保存用户的持久偏好与期望。
- `_PLAN_EXTRACTION_PROMPT`（计划提取）：在所有 todo 完成时触发一次的运行，产出两项内容。**Part 1** 通过 `knowledge` 工具（`action="write"`）把结构化 JSON 知识写入 `workspace/knowledge/plans/<plan-name>/`，在 task、wave、plan 三个层级分别记录 `failure_set` / `success_path` / `method`。**Part 2** 通过 `skill_manage` 更新技能库（原先独立的技能复盘指引并入此处）。其上下文来自 `_build_plan_context`：计划文件、todo 列表以及本会话的 subagent runs（仅 `result_text` / `outcome` / 任务）。
- **压缩后待办更新：** 当 `compression_todo_update_enabled`（默认开启）且本次压缩确实丢弃了消息时，异步路径会 fire-and-forget 一个专用 nudge agent（`_COMPRESSION_TODO_PROMPT`）：其图运行在派生会话键（`<id>::compression-todo`，因此 `IterationBudget` / `ToolGuardrails` 状态绝不会碰到主会话）下，工具集只有一个带 metadata 标记的 `todowrite` 垫片（`todo_update: True`，经 `_NudgeLimitTool(allowed_metadata_key="todo_update")` 放行）且绑定主会话。它根据被丢弃的对话片段核对会话待办列表——把实际完成的条目标为 `completed` / `cancelled`，把有据可查的新工作加为 `pending`，并用 `todowrite` 写回**完整**列表。它绝不阻塞或影响压缩；每会话 `compression_todo_update_lock` 防重入，同步路径仅在已有事件循环时调度。

▶️ 完整文档：[docs/summarization/README.md](../../docs/summarization/README.md) · [中文](../../docs/summarization/README.zh.md) · [한국어](../../docs/summarization/README.ko.md) · [日本語](../../docs/summarization/README.ja.md)

> 预算中间件的类默认值 `max_iterations` 为 50；*实际注册*值是 90（主）与 60（worker）。本文档旧版本声称预算为 10——那是错的。

### MaxTokensBoostMiddleware

**模块：** `agent/middlewares/max_tokens_boost/core.py` · **类：** `MaxTokensBoostMiddleware(AgentMiddleware)`
**钩子：** `wrap_model_call` / `awrap_model_call`

从**工具调用截断**中恢复：当模型调用返回 `finish_reason == "length"`（OpenAI）/
`stop_reason == "max_tokens"`（Anthropic）且响应携带工具调用时，说明工具调用的
JSON 本身被截断。中间件在 `wrap_model_call` / `awrap_model_call` 内以提升后的
`max_tokens = base × 2^attempt`（上限 32768；最多重试 3 次）重新调用 handler，
让模型补全完整的工具调用载荷。base 按三层降级解析（取第一个正值）：

1. 本次调用自身的 `request.model_settings["max_tokens"]` —— 即当前调用的真实
   上限，因此调用配置了更高 max_tokens 时，重呼不会从更低的默认值重新起步；
2. 环境变量 `MAIN_LLM_OUTPUT_MAX_TOKEN`（默认 8192）；
3. 硬编码默认值 8192。

被截断的中间结果被丢弃——agent 循环只看到最终结果，因此不会有截断内容写入
checkpointer，且 IterationBudget 每个外层模型调用只计 1 次。

- **纯文本截断**（无工具调用）不在此处理——由服务层 `StreamTurn` 外层循环负责
  （注入续写 HumanMessage）。
- **流式重呼会剥离 callbacks**：第一次调用的截断 token 已经流式发给客户端；
  每次重呼前中间件会移除 `request.config["callbacks"]`，避免重复输出，并在
  `finally` 中恢复原始 callbacks（即使异常也恢复）。流式/非流式的判定读取
  `StreamTurn.run()` 按会话设置的 `is_stream_turn` 标志——子代理（ainvoke）
  永远不带该标志，始终走非流式路径。
- `_extract_ai_message` 同时处理裸 `AIMessage` 结果与携带 `.messages` 的
  `ModelRequest` 形态响应对象。
- **思考预算交互：** 当 `MAIN_LLM_ENABLE_THINKING=true` 时，
  `models/LLMs/main_llm.py::apply_thinking_budget` 会预先把请求的 `max_tokens`
  膨胀为 `OUTPUT_MAX_TOKEN + 思考预算`（与本中间件共享 `MAIN_LLM_OUTPUT_MAX_TOKEN`
  环境变量 / 8192 默认值），因此第一层 boost base 已经从膨胀后的输出上限起步。
- **服务层诊断：** 流失败时，`server/service/stream_diag.py` 统计块数/字节数与
  首块耗时，并把摘要附加到重新抛出的异常上——是对上述中间件级恢复的补充观测。

### OutputRepetitionGuard 与 RepetitionGuardWrapper

**模块：** `agent/middlewares/output_repetition_guard/core.py` · **类：** `OutputRepetitionGuard(AgentMiddleware)`
**钩子：** `before_agent` / `abefore_agent`、`wrap_model_call` / `awrap_model_call`

事后式的输出重复检测器，带 `WARN → HALT` 升级。从 `agent.middlewares.output_repetition_guard.core` 导出，并被 `agent/middlewares/__init__.py` 再导出；在主 Agent（逐调用拦截，与下方的包装器互补）与 worker 流水线中**都有**注册。

主 Agent 的同类检测由 **`RepetitionGuardWrapper`**（`agent/wrapper/repetition_guard.py`）完成：它包装编译后的图，在流式层面拦截（外加 `ainvoke` 事后兜底），复用相同的状态键与默认值。两处注册均传入 `phantom_stream_guard=True`。

**检测层**

- **跨调用重复** —— 对可见输出的最后 `_TAIL_CHARS = 500` 个字符取 MD5，与滚动历史（`_MAX_HISTORY = 30`）比较。连续 `warn_after = 2` 次相同输出 → WARN（`AIMessage` 提醒）；`max_identical_outputs = 3` 次 → HALT，返回终止 `AIMessage` 并置粘性终止标志。
- **单次输出内部重复**：
  - 句子/行重复占比 > `internal_repeat_ratio = 0.6`（且分段数 ≥ `internal_min_lines = 6`）；
  - 出现 ≥ `char_run_min = 8` 个连续相同的非空白字符；
  - 2–10 字符的短语重复 ≥ 5 次。

  内部警告按标签每会话只触发一次。
- 少于 `_MIN_CONTENT_LENGTH = 20` 字符的内容跳过；含工具调用的模型响应整体跳过（工具循环结束后会再次检查）。
- **推理内容单独跟踪**（`additional_kwargs` 中的 `reasoning_content` / `reasoning` / `reasoning_text`，以及内联的 `<think>` / `<thinking>` / `<reasoning>` 块——会被提取并从可见内容中剥离）。

**流式截断**由 `RepetitionGuardWrapper`（`agent/wrapper/repetition_guard.py`）负责：它拦截图的 `astream`，复用本模块的内部重复检测器、同一套按会话的内部警告去重门以及共享的 `_STREAM_WARNING` 文案。原模块级辅助函数 `check_stream_repetition` 没有任何生产调用者，已删除。

**Worker 清理：** 子会话结束时，`SESSION_STATE_KEYS`（六个键）会从 `state_register_mem` 中删除。

### ContextLimitGuardWrapper

**模块：** `agent/wrapper/context_limit.py` · **类：** `ContextLimitGuardWrapper`

一个图包装器（与 `RepetitionGuardWrapper` 同类），**不是**中间件。`agent/core.py` 通过可插拔注册表应用默认包装链（`agent/wrapper/registry.py::apply_graph_wrappers`，由内到外），因此编译后的图成为 `ContextLimitGuardWrapper(RepetitionGuardWrapper(graph))`——ContextLimit 位于重复防护的**外侧**，在重复过滤之前看到流式块。它补上了中间件的流式盲区：中间件看不到流中产生的块，而响应后的溢出信号也无法回溯压缩上下文。

**防御一——模型调用边界强制压缩：** 从 `messages` 块中捕获真实的 `usage_metadata` 输入/输出 token；在每个模型调用边界（`updates` 模式）及流结束时，同时按当前调用（仅 `input_tokens`）与预测视图（`input + output`，因为输出会成为下次调用的输入）对照上下文窗口的 `COMPRESSION_TRIGGER_RATIO`（80 %）阈值。达到/越过阈值时，向 `state_register_mem` 写入 Summarization 的强制恢复键（`summarization_force_recovery`），使下一次调用前检查执行压缩，而不被冷却 / 尝试上限的防抖闸门跳过。

**防御二——流中输出预算：** 累积的模型文本按 CJK 感知的 `estimate_text_tokens` 估算（CJK 字符 `// 2`、其余字符 `// 4`；纯 ASCII 退化为与旧 `len // 4` 相同的结果），每 `check_interval = 20` 个块检查一次；一旦估算值超过窗口的 `output_cut_ratio = 0.20`，后续文本块不再转发给客户端（工具调用块仍然通过），改为发出一次性截断标记（`"[System notice: the response exceeded the mid-stream output budget and was truncated.]"`）。图内仍累积完整的 `AIMessage`——被截掉的尾部正是下一轮压缩要移除的内容。

`ainvoke` 原样委托（Summarization 的 T1–T3 触发点已覆盖非流式路径）；未知属性委托给内部图。构造函数：`(inner, context_window, output_cut_ratio=0.20, check_interval=20)`——`context_window` 即 `MAIN_LLM_MAX_TOKEN`，与 Summarization 触发点同源。

---

## 共享状态系统

所有跨调用的中间件状态都按会话隔离，存放在两个寄存器加一个定时器注册表中：

| 寄存器 | 底层存储 | 说明 |
|---|---|---|
| `state_register_mem`（`StateRegisterMeM`） | 内存字典 | 易失；`_initialized` 守卫保证进程启动时只重置一次 |
| `state_register_db`（`StateRegisterDB`） | SQLite（`src/data/state_register.db`） | 重启后仍保留；不支持 `clear_session`（返回 `False`）；提供 `get_all_session_ids` |
| `timer_call_register`（`TimerCallRegister`） | asyncio 定时器 | `register(session_id, name, callback, args, minutes 1–60, execute_now=False)` |

通用接口（`runtime/session/state_register.py`）：`set_state`、`get_state`、`get_all_states`、`delete_state`、`clear_session`、`has_session`、`has_key`、`update_states`。

### 命名空间约定

| 键 | 归属 | 寄存器 |
|---|---|---|
| `system_prompt` | system_prompt_injection / Summarization | mem + db |
| `nudge_plan_extraction_fired` | 压缩时 nudge 调度器（Summarization → `summarization/nudges.py`） | db |
| `nudge_review_memory_lock`、`nudge_plan_extraction_lock` | 压缩时 nudge 调度器（Summarization → `summarization/nudges.py`） | mem |
| `iteration_budget`、`iteration_budget_used` | IterationBudget | mem |
| `tool_guardrail_state` | ToolGuardrails | mem |
| `summarization_*` 键（压缩计数器、无效连击、上次 token/策略、跳过 LLM 标志、恢复状态、上次用户提问） | Summarization | mem |
| `heartbeat_iter`、`heartbeat_tool`、`heartbeat_stale`、`heartbeat_killed`、`heartbeat_skip`、`_last_heartbeat_iter`、`_last_heartbeat_tool` | HeartbeatStaleness | mem |
| OutputRepetitionGuard 的键（`SESSION_STATE_KEYS`，六个） | OutputRepetitionGuard / RepetitionGuardWrapper | mem |
| `llm_stale_streak`、`llm_fallback_index` | LLMRetryMiddleware | mem |
| `_multimodal_trying_native`、`_multimodal_native_model` | MultimodalProcessor（写入）→ LLMRetryMiddleware（消费） | mem |
| `llm_content_filter_blocked`、`llm_content_filter_terminated` | 流式层（写入）→ LLMRetryMiddleware（消费） | mem |
| `llm_partial_stream_stub`、`llm_partial_stream_cause` | 流式层（写入）→ LLMRetryMiddleware（消费） | mem |
| `summarization_force_recovery` | ContextLimitGuardWrapper（写入）→ Summarization（消费） | mem |
| `hitl:` 前缀键（`_STATE_PREFIX = "hitl"`） | HumanInTheLoop | mem |

---

## 配置

### 环境变量与配置项

| 配置项 | 位置 | 作用 |
|---|---|---|
| `MAIN_LLM_MAX_TOKEN` | `.env` → `models/LLMs/main_llm.py` | 主 Agent 摘要触发点 = 该值的 80 %；同时作为 `main_llm_context_window` 与 `ContextLimitGuardWrapper.context_window` 传入。必须 >= 131072 (128K)：token guard 会在启动与图构建阶段阻止低于该值 |
| `MAIN_LLM_OUTPUT_MAX_TOKEN` | `.env` → `models/LLMs/main_llm.py` | 输出 token 预算（默认 8192）：MaxTokensBoost boost base 的第 2 层，也是思考预算膨胀叠加的基数 |
| `FALLBACK_LLM_{i}_{PROVIDER,NAME,API_KEY,API_BASE}` | `.env` → `build_fallback_chain()` | `LLMRetryMiddleware` 的模型回退链候选（i = 1…，遇到第一个缺失的 `NAME` 即停止） |

### 特性配置（`config/features/agent_side/`）

中间件调优存放在按对象拆分的 TypedDict 模块中——环境变量只提供上表的两个 LLM 预算。本层消费的旋钮：

| 模块 | 旋钮 |
|---|---|
| `iteration_budget.py` | `main_agent_max_iterations=90`、`worker_max_iterations=60`、`default_max_iterations=50` |
| `media_pipeline.py` | `main_llm_native_multimodal="auto"`（`"true"` / `"false"` / `"auto"`；其它值走 fail-safe 技能路径）、`main_llm_silent_degradation_detection=True`、`max_media_bytes=20 MiB`、`multimodal_temp_retention_days=7` |
| `summarization.py` | `compression_trigger_ratio=0.80`，以及 `Summarization` 与 `summarization/nudges.py` 读取的保留/尝试/冷却/阈值常量 |
| `tool_result_eviction.py` | `enabled=True`、`evict_threshold_chars=20 000`、预览头尾各 5 行、`human_evict_enabled=True`、`human_evict_threshold_chars=200 000` |
| `tool_guardrails.py` | ToolGuardrails 小节列出的五类病态阈值与恢复默认值 |
| `heartbeat_staleness.py` | `heartbeat_interval_minutes=1`、`stale_cycles_idle=7`、`stale_cycles_in_tool=20` |
| `repetition_guard.py` | `max_identical_outputs=3`、`warn_after=2`、`internal_repeat_ratio=0.6`、`internal_min_lines=6`、`char_run_min=8`、`tail_chars=500`、`max_history=30` |
| `max_tokens_boost.py` | `base_max_tokens`（`MAIN_LLM_OUTPUT_MAX_TOKEN`，默认 8192）、`default_max_tokens=8192`、`max_cap=32 768`、`max_retries=3` |
| `llm_retry.py` | `max_retries=3`、`base_delay=2.0`、`max_delay=60.0`、`jitter=0.3`、`stale_giveup_threshold=5` |
| `context_guard.py` | `output_cut_ratio=0.20`、`check_interval=20` |

> **相关但独立：** 各工具的超时是绑定特性注册表的模块常量（`config/features/agent_side/tools_timeouts.py` 中的 `TOOLS_TIMEOUTS`）——`WEB_SEARCH_TIMEOUT = 15`（`agent/tools/web_search.py`）、`TERMINAL_TIMEOUT = 30`（`agent/tools/terminal.py`）、`PYTHON_REPL_TIMEOUT = 30`（`agent/tools/python_repl.py`；超时会杀死子进程）。`TOOL_CALL_TIMEOUT_MINUTES`（默认 5）是 `sherry.jsonc` 设置，经 `config/sherry_settings.py` 贯通并由配置服务对外暴露，但**没有任何工具执行路径消费它**——它不是生效的超时旋钮。摘要管线从 `config/features/agent_side/summarization.py` 读取其常量。

### 构建示例（节选）

此处并未展示全部已注册中间件——完整主 Agent 列表见[中间件链](#中间件链)。

```python
from langchain.agents import create_agent
from agent.middlewares import (
    system_prompt_injection,
    MultimodalProcessor,
    IterationBudget,
    ToolGuardrails,
    ToolCallNormalize,
    HeartbeatStaleness,
    HumanInTheLoop,
    HITLConfig,
    MaxTokensBoostMiddleware,
    Summarization,
)

agent = create_agent(
    model=main_llm,
    tools=tools,
    middleware=[
        system_prompt_injection,  # @dynamic_prompt：系统提示词注入
        MultimodalProcessor(),  # 多模态输入规范化
        IterationBudget(90),  # 回合级调用预算
        ToolGuardrails(),  # 失败病理检测
        ToolCallNormalize(),  # tool_use/tool_result 修复
        PathGuard(),  # 路径参数筛查
        HeartbeatStaleness(),  # 卡死回合看门狗
        HumanInTheLoop(HITLConfig()),  # 审批门控
        Summarization(  # 上下文压缩（最内层）
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
| `MaxTokensBoostMiddleware` | （环境变量） | base：请求 `max_tokens` → `MAIN_LLM_OUTPUT_MAX_TOKEN`（8192）→ 8192，上限 32768，重试 3 次 | 默认值 |
| `LLMRetryMiddleware` | `config: LLMRetryConfig` | `max_retries=3`、`base_delay=2.0`、`max_delay=60.0`、`jitter=0.3`、`stale_giveup_threshold=5` | 默认值（外加来自 `FALLBACK_LLM_*` 的 `fallback_chain`） |

---

## 生命周期与数据流

### 单回合详解

```
用户回合到达
│
├─ before_agent（列表顺序）
│   MultimodalProcessor → IterationBudget → ToolGuardrails → OutputRepetitionGuard
│   → HeartbeatStaleness → HumanInTheLoop → Summarization
│   · MultimodalProcessor  先存盘媒体，再按配置 / 能力缓存保留原生块或注入技能提示
│   · IterationBudget  重置预算计数器
│   · ToolGuardrails  重置回合级护栏状态
│   · OutputRepetitionGuard  重置回合级重复状态
│   · HeartbeatStaleness  重置状态键 + 启动 1 分钟心跳定时器
│   · HumanInTheLoop  重置回合级中断标志
│   · Summarization  重置压缩计数器
│
├─ 循环：模型调用
│   ├─ before_model
│   │   · ContextEvictionMiddleware  给末条超长 HumanMessage 打标（P1-9）
│   │   · ToolCallNormalize  sanitize_tool_use_result_pairing + RemoveMessage 重写
│   │   · SubagentCompletionDrainMiddleware  排出排队的完成载体
│   │   · TaskIntentMiddleware  任务意图 / 计划活跃引导（回合的第一次调用）
│   ├─ wrap_model_call（最外层 → 最内层）
│   │   · system_prompt_injection  注入系统提示（装饰器调用 request.override）
│   │   · MultimodalProcessor  仅 auto 模式：把不支持的媒体块替换为文本占位符（仅请求副本）
│   │   · IterationBudget  消耗 1；耗尽时返回终止 AIMessage
│   │   · ContextEvictionMiddleware  把被驱逐的人类消息换成预览（P1-9）
│   │   · OutputRepetitionGuard  逐调用重复拦截
│   │   · MaxTokensBoostMiddleware  工具调用被截断时重呼
│   │   · HeartbeatStaleness  已杀死则抛 HeartbeatTimeoutError；否则 heartbeat_iter += 1
│   │   · LLMRetryMiddleware  熔断器检查；分类重试 + 退避；回退 /
│   │                        内容过滤 / 部分流桩标志消费
│   │   · Summarization  视情况压缩历史（非 LLM 策略 + 辅助 LLM），防抖计数
│   ├─ LLM 响应
│   └─ after_model（逆序；落库先跑）
│       · MessagePersistenceMiddleware  把新 human/ai/tool 消息增量落库到 MesMemory（水位写一次）
│       · HumanInTheLoop  策略检查；必要时 interrupt()；阻止 → 错误 ToolMessage
│
├─ 循环：工具调用（每次调用）
│   └─ wrap_tool_call
│       · IterationBudget  消耗 1；耗尽时返回错误 ToolMessage
│       · ToolGuardrails  预检 block/halt → 执行 → 评估 → warn/block/halt
│       · ContextEvictionMiddleware  把原始结果替换为驱逐/切片后的视图
│       · PathGuard  在工具执行前拒绝穿越 / 硬拒绝的路径参数
│       · HeartbeatStaleness  已杀死则抛出；设置 heartbeat_tool，返回后清除
│       · HumanInTheLoop  拒绝审批被拒/超时的调用
│       · MessagePersistenceMiddleware  工具返回的 ToolMessage 立即落库
│         （最内层 wrap；HITL 拒绝会绕过它，改在下一边界落库）
│
└─ after_agent（逆序）
    HeartbeatStaleness → MultimodalProcessor → TodoContinuationEnforcer
    · HeartbeatStaleness  停止心跳定时器
    · MultimodalProcessor  清理 mutil_temp（> 7 天 / 非数字文件名）
    · TodoContinuationEnforcer  仍有未完成 todo 时以 fire-and-forget 注入续作提示
    · （system_prompt_injection 不实现任何生命周期钩子；消息持久化在自己的
       after_model 钩子中运行，记忆复盘 / 计划提取 nudge 改在 Summarization
       的压缩路径内触发。）
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
├── base.py                      # require_session_id / args_hash 辅助
├── llm_capability_cache.py      # 进程级原生多模态能力缓存
├── context_eviction/            # ContextEvictionMiddleware（P0-2/P2-4 + P1-9）
│   ├── __init__.py              # 导出 ContextEvictionMiddleware
│   └── core.py                  # ContextEvictionMiddleware
├── system_prompt/               # @dynamic_prompt 系统提示词注入
│   ├── __init__.py              # 仅导出 system_prompt_injection
│   └── core.py                  # system_prompt_injection + _get_and_reload_system_prompt
├── heartbeat_staleness/         # HeartbeatStaleness
│   ├── __init__.py              # 导出 HeartbeatStaleness
│   └── core.py                  # HeartbeatStaleness
├── humanInTheLoop/              # HumanInTheLoop + HITLConfig（有自己的 README）
│   ├── __init__.py              # 导出 HumanInTheLoop、HITLConfig
│   ├── types.py                 # 枚举 + 配置数据类（_STATE_PREFIX = "hitl"）
│   ├── detection.py             # 硬红线 / 危险命令模式
│   ├── approval.py              # ApprovalPipeline
│   ├── approval_scope.py        # 持久审批的操作者作用域解析
│   ├── approval_store.py        # ToolApprovalStore（SRC_DIR/data/approvals.json，CAS）
│   ├── gates.py                 # WriteApprovalGate、InterruptManager、MCPElicitationConsent、
│   │                            # KanbanTriage、PairingStore、SlashConfirm
│   ├── strategies.py            # ToolApprovalHandler 分支 + ApprovalHandlerRegistry
│   └── core.py                  # HumanInTheLoop
├── iteration_budget/            # IterationBudget
│   ├── __init__.py              # 导出 IterationBudget
│   └── core.py                  # IterationBudget
├── llm_retry/                   # LLMRetryMiddleware（含 LLMRetryConfig、FallbackCandidate、ContentFilterError）
│   ├── __init__.py              # 仅导出 LLMRetryMiddleware
│   └── core.py                  # LLMRetryMiddleware、LLMRetryConfig、FallbackCandidate、
│                                # ContentFilterError
├── max_tokens_boost/            # MaxTokensBoostMiddleware（工具调用截断重呼）
│   ├── __init__.py              # 导出 MaxTokensBoostMiddleware
│   └── core.py                  # MaxTokensBoostMiddleware
├── media_pipeline/              # MultimodalProcessor
│   ├── __init__.py              # 导出 MultimodalProcessor
│   ├── core.py                  # MultimodalProcessor
│   ├── degradation.py           # 静默降级检测器（自述失明）
│   ├── fallback.py              # 技能路径回退（媒体提示 + 请求改写）
│   ├── media_handlers.py        # 分媒体类型策略 + MediaPaths（含体积上限）
│   ├── scrub.py                 # 每请求擦洗不支持的媒体块
│   └── mixins.py                # BeforeAgentHooksMixin / AfterAgentHooksMixin（共享）
├── message_persistence/         # MessagePersistenceMiddleware
│   ├── __init__.py              # 导出 MessagePersistenceMiddleware
│   ├── core.py                  # MessagePersistenceMiddleware（after_model / aafter_model）
│   └── prepare.py               # 落库批次过滤 + HITL 拒绝重挂
├── output_repetition_guard/     # OutputRepetitionGuard
│   ├── __init__.py              # 导出 OutputRepetitionGuard
│   ├── core.py                  # OutputRepetitionGuard（由 __init__.py 再导出）
│   ├── repetition_detectors.py  # 纯重复检测原语
│   └── repetition_state.py      # 会话级重复状态辅助
├── path_guard/                  # PathGuard（工具调用的路径参数筛查）
│   ├── __init__.py              # 仅导出 PathGuard
│   └── core.py                  # PathGuard
├── subagent_completion_drain/   # SubagentCompletionDrainMiddleware
│   ├── __init__.py              # 导出 SubagentCompletionDrainMiddleware
│   └── core.py                  # SubagentCompletionDrainMiddleware
├── summarization/               # Summarization
│   ├── __init__.py              # 导出 Summarization
│   ├── core.py                  # Summarization
│   ├── summarization_components.py # Summarization 共享组件（_FORCE_RECOVERY_KEY 等）
│   ├── compaction_lock.py       # SQLite 压缩锁（TTL、fail-open）
│   ├── media_offload.py         # 压缩时内联媒体归档
│   ├── memory_flush.py          # 压缩前记忆落盘
│   ├── nudges.py                # 压缩时 nudge 调度 + 提示词
│   ├── plan_context.py          # 计划提取 nudge 的活跃计划检测
│   └── summary_doc.py           # SummaryDoc 模式 + 确定性 Markdown 渲染
├── task_intent/                 # TaskIntentMiddleware
│   ├── __init__.py              # 导出 TaskIntentMiddleware
│   └── core.py                  # TaskIntentMiddleware
├── todo_continuation/           # TodoContinuationEnforcer
│   ├── __init__.py              # 导出 TodoContinuationEnforcer
│   └── core.py                  # TodoContinuationEnforcer
├── tool_call_normalize/         # ToolCallNormalize
│   ├── __init__.py              # 导出 ToolCallNormalize
│   └── core.py                  # ToolCallNormalize
├── tool_guardrails/             # ToolGuardrails
│   ├── __init__.py              # 导出 ToolGuardrails
│   └── core.py                  # ToolGuardrails
└── README.md                    # 本文件（+ .zh / .ja / .ko 变体）

agent/wrapper/registry.py             # 有序、可插拔的图包装链
agent/wrapper/repetition_guard.py     # RepetitionGuardWrapper（位于本包之外）
agent/wrapper/context_limit.py        # ContextLimitGuardWrapper（位于本包之外）
```

### 导出（`__init__.py`）

```python
from agent.middlewares import (
    Summarization,
    LLMRetryMiddleware,
    LLMRetryConfig,
    FallbackCandidate,
    ContentFilterError,
    OutputRepetitionGuard,
    MaxTokensBoostMiddleware,
    ToolGuardrails,
    ContextEvictionMiddleware,
    IterationBudget,
    system_prompt_injection,
    ToolCallNormalize,
    PathGuard,
    HeartbeatStaleness,
    MultimodalProcessor,
    HumanInTheLoop,
    HITLConfig,
    MessagePersistenceMiddleware,
    llm_capability_cache,  # 模块：进程级原生多模态能力缓存
)
# 共享辅助函数同样导出：BeforeAgentHooksMixin、
# AfterAgentHooksMixin、require_session_id、args_hash。
# SubagentCompletionDrainMiddleware / TaskIntentMiddleware / TodoContinuationEnforcer
# 由 agent/core.py 从各自子模块导入——包不再导出它们。
```
