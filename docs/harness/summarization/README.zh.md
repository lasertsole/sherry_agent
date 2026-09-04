# 🗜️ 上下文压缩：Summarization 中间件

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> Agent 如何让长对话保持在模型的上下文窗口之内：一个确定性的 token 估算器负责拉响警报，非 LLM 策略免费削减工具输出的噪音，只有当这些还不够时，辅助 LLM 才会把旧的对话轮次重写成一份结构化的检查点；同时还有防抖动护栏，保证压缩永远不会失控打转。

事实来源：`agent/middlewares/summarization.py`、`pub_func/message/estimate_msg_tokens.py`、`pub_func/message/tool_output_dedup.py`、`pub_func/message/tool_output_prune.py`、`pub_func/message/target_truncation.py`、`pub_func/message/turn_utils.py`、`config/num.py`，外加两处注册点 `agent/core.py` 与 `agent/tools/subagent/spawn/core.py`。本文档中的每一处行号与常量都已对照这些代码逐一核实。

## 目录

- [概览](#-概览)
- [运行位置：每次模型调用流程](#-运行位置每次模型调用流程)
- [触发条件：三道闸门](#-触发条件三道闸门)
- [Token 估算（无分词器）](#-token-估算无分词器)
- [保留预算与截断点](#-保留预算与截断点)
- [`_apply_compression` 内的压缩管线](#-_apply_compression-内的压缩管线)
- [LLM 摘要：提示词、链式续写与回退](#-llm-摘要提示词链式续写与回退)
- [静态回退（无 LLM 摘要）](#-静态回退无-llm-摘要)
- [非 LLM 策略](#-非-llm-策略)
- [输出：摘要消息对](#-输出摘要消息对)
- [防抖动与退化恢复](#-防抖动与退化恢复)
- [系统提示词刷新](#-系统提示词刷新)
- [注册位置](#-注册位置)
- [配置参考](#-配置参考)
- [测试](#-测试)
- [⚠️ 诚实声明与局限](#%EF%B8%8F-诚实声明与局限)

## 🎯 概览

`Summarization`（`agent/middlewares/summarization.py`，类定义在第 402 行）是一个**从零实现**的 `AgentMiddleware`，**并不**继承 LangChain 内置的 `SummarizationMiddleware`。全部压缩逻辑都是自包含的：触发检查、截断点判定、摘要生成、多策略缩减管线，以及退化监控。

它的职责：当对话增长超过阈值时，把消息历史的旧前缀替换成一份紧凑的检查点，同时逐字保留最近的上下文。每次压缩之后，历史始终呈如下形态：

```
HumanMessage("What did we do so far?")
AIMessage(<summary>, lc_source="summarization")
<recent turns preserved verbatim>
```

由于替换物是一个 Human/AI 消息对，模型永远不会看到两条连续的同角色消息，因此不需要配对修复。

共有两处注册：

| 注册位置 | 触发条件 | LLM | `need_update_system_prompt` |
| :--- | :------ | :-- | :-------------------------- |
| 主 agent（`agent/core.py:152`） | `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `True` |
| 工作代理/子代理（`agent/tools/subagent/spawn/core.py:755`） | `("messages", 40)` **或** `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `False`（默认） |

两处都传入 `main_llm_context_window=main_llm_max_tokens`（来自 `MAIN_LLM_MAX_TOKEN`）以及 `keep=("messages", 10)`。

## 🧭 运行位置：每次模型调用流程

该中间件挂接 `before_agent`/`abefore_agent`（计数器重置）与 `wrap_model_call`/`awrap_model_call`（第 1188–1262 行）。在中间件链中它位于**最内层，也就是离 LLM 最近的位置**，因此它对消息的改写是模型调用前的最后一步。

```
wrap_model_call(request, handler)
│
├─ 1. _check_last_turn_ratio      last turn ≥ 50% of tokens? → flag it
├─ 2. _should_skip_compression    max attempts reached / LLM marked ineffective?
│        └─ yes → call handler directly, monitor response, return
├─ 3. _preemptive_check           pressure = est_tokens / context_window
│        ├─ ≥ 0.80            → "compact"
│        ├─ ≥ 0.70            → "truncate_only"
│        └─ else              → None
├─ 4. if truncate_only|compact:
│        _preemptive_truncate    shrink oversized ToolMessages (> 2000 chars),
│                                no LLM call, override request messages
├─ 5. need_compress = (action == "compact") OR configured trigger fires
├─ 6. if need_compress: _apply_compression(...)   exceptions logged, never fatal
├─ 7. response = handler(request)
└─ 8. _monitor_degradation(response)   count empty responses after compaction
```

完整管线在异步路径中由 `_aapply_compression` 镜像实现（第 1087–1153 行），两者语义完全一致。

## 🚦 触发条件：三道闸门

**闸门 1：配置的触发条件**（`_check_trigger`，第 478 行）。每个子句要么是 `("messages", N)`（历史长度 ≥ N），要么是 `("tokens", N)`（有效 token 数 ≥ N）。多个子句构成 OR 关系。

**闸门 2：抢先式压力检查**（`_preemptive_check`，第 491 行）。要求已设置 `main_llm_context_window`；计算 `pressure = effective_tokens / context_window` 并返回：

- `pressure ≥ COMPRESSION_TRIGGER_RATIO (0.80)` 时返回 `"compact"`，本次调用执行完整压缩；
- `pressure ≥ PREEMPTIVE_TRUNCATE_RATIO (0.70)` 时返回 `"truncate_only"`，只做不依赖 LLM 的工具输出缩减。

**闸门 3：最后一轮占比**（`_check_last_turn_ratio`，第 581 行）。如果最后一个用户轮次单独就占全部 token 的 ≥ `LAST_TURN_RATIO_THRESHOLD (0.5)`，则设置 `_compress_last_turn`：截断点逻辑将被允许把摘要**延伸进**最后一轮，而不是保护它。最后的用户问题会被暂存到会话状态中，用作恢复上下文。

"有效 token 数" = `max(local estimate, last AIMessage's reported usage_metadata.total_tokens)`。只要 API 上报的数字存在，它就胜出，因为那才是地面真值（第 455–461、478–511 行）。

## 🪙 Token 估算（无分词器）

`pub_func/message/estimate_msg_tokens.py` 刻意不使用任何分词器，并且完全确定：

```python
tokens = (content chars            # str content, or len(json.dumps(content))
        + Σ tool_call name/args chars
        + tool_call_id chars) // CHARS_PER_TOKEN   # CHARS_PER_TOKEN = 4
```

它速度快、跨次运行稳定（相同输入 → 相同数字 → 测试可复现），并且有意采取保守近似。触发/预算路径上的任何环节都不依赖模型分词器。

## 💰 保留预算与截断点

**预算**（`_calculate_preserve_budget`，第 467 行）：

```
budget = clamp(context_window × PRESERVE_RATIO(0.25), MIN_PRESERVE_TOKENS(2000), MAX_PRESERVE_TOKENS(15000))
without a context window → MIN_PRESERVE_TOKENS (2000)
```

**截断点**（`_determine_cutoff`，第 668 行）决定历史的哪一段尾部被逐字保留：

1. 把历史切分成轮次（`split_into_turns`），然后**从最新的一轮向前回溯**，累加各轮大小直到预算用满。
2. 一轮放不下时，可以在轮次中间再切分（`split_turn`），把剩余预算用得分毫不差。
3. `_adjust_for_orphan_pairs`（第 698 行）随后把截断点**向后**回退，直到没有任何 `ToolMessage` 与它的 `AIMessage` 工具调用分离：工具调用被摘要掉而结果却留下，会直接构成 API 错误。
4. 除非设置了 `_compress_last_turn`，否则截断点会被钳制，绝不越过最后一条 `HumanMessage`：当前的问题总是被逐字保留。

⚠️ **noop 陷阱：** 如果整段历史都在预算之内，回溯永远不会移动截断点，它保持为 `0`，本轮不做任何摘要（`cutoff == 0 → "noop"`，第 1045–1047/1117–1119 行）。在默认下限 `MIN_PRESERVE_TOKENS = 2000` 之下，估算 token 数少于约 2000 的历史永远不会被 LLM 摘要。集成测试会注入一个较小的 `main_llm_context_window`（例如 8 000），以便确定性地走到摘要路径。

## 🔁 `_apply_compression` 内的压缩管线

`_apply_compression`（第 1015 行；异步孪生实现位于第 1087 行）按顺序执行：

1. **捕获恢复上下文**（`_capture_recovery_context`，第 904 行）：最后一条用户请求（≤ 800 字符）和文件操作棘轮，即从 `read`/`write` 族工具调用中提取的路径，与上一轮的集合合并（读过的会被记住，修改过的文件绝不会降级回只读）。
2. **非 LLM 策略**（`_run_non_llm_strategies`，第 851 行）：`dedup → prune → target truncate`（下文详述）。这些是免费的，不调用任何模型。
3. **是否调用 LLM 的判定**（第 1030 行）：

   ```
   if tokens_after_non_llm > budget × 2  OR  skip_llm  OR  nothing was reduced:
       summarize [0:cutoff] and rebuild   → strategy "llm_summary" / "fallback"
   else:
       keep as-is                          → strategy "non_llm_sufficient"
   ```

   非 LLM 缩减享有第一次机会；只有当历史仍然超过保留预算的两倍（或者 LLM 摘要已被防抖动调节器禁用，或者非 LLM 策略一无所减）时，才会动用辅助 LLM。
4. **激进兜底**（第 1052 行）：如果结果*依然* > `budget × 2`，每条超过 `AGGRESSIVE_TRUNCATE_CHARS (1000)` 字符的 `ToolMessage` 都会被硬切。
5. **摘要自我截断**（`_truncate_summary_messages`，第 957 行）：任何已存在的摘要消息（`lc_source == "summarization"`）若超过 `SUMMARY_TOTAL_MAX_CHARS (16 000)` 字符，就按头部 30% / 尾部 30% 重新截断。
6. **恢复信息注入**（`_inject_recovery_context`，第 922 行）：捕获的文件操作棘轮被重写进摘要的 `## Relevant Files` 小节，因此检查点总是携带一份最新的读/改文件清单。
7. **记账**（`_record_compression`，第 635 行），最后执行 `request.override(messages=..., system_message=...)`。

每一种失败模式都是 fail-open：如果 `_apply_compression` 抛出异常，异常会被记录日志（第 1217 行），原始请求原样继续。压缩坏掉绝不会弄坏这一轮对话。

## 📝 LLM 摘要：提示词、链式续写与回退

`_create_summary` / `_acreate_summary`（第 768–816 行）：

1. **序列化**（`_serialize_for_summary`，第 167 行）：每条消息变成一行带标签的文本：`[User]:`（≤ 2000 字符）、`[Assistant]:`（≤ 2000 字符）、`[Assistant tool call]: name(args ≤ 500 chars)`、`[Tool result|Tool error] (id):`（≤ 1800 字符 + 省略标记）。
2. **链接上一个检查点**（`_extract_previous_summary`，第 734 行）：找到携带 `additional_kwargs["lc_source"] == "summarization"` 的最新 `AIMessage`，并抽出它的 `<summary>…</summary>` 正文。如果存在，提示词就从 `_SUMMARY_PROMPT_FIRST` 换成 `conversation + prior-summary + _SUMMARY_PROMPT_UPDATE`（向前传递目标/约束/决策，冲突以最新为准，FIFO 数量上限），而不是 `_SUMMARY_PROMPT_FIRST`。
3. **调用**辅助模型时附带 `config={"metadata": {"lc_source": "summarization"}}`，让下游工具能识别出摘要调用。
4. **护栏：** 响应为空或短于 50 字符时，回退到确定性摘要（第 785 行）；任何异常同样如此（第 789 行）。失败时永远不让 LLM 拥有最终决定权。

提示词模板（`_SUMMARY_TEMPLATE`，第 99 行）固定了 Markdown 骨架：*Latest Unresolved User Request / Goal / Constraints & Preferences / Progress (Completed ≤ 5 · In Progress · Blocked) / Key Decisions ≤ 5 / Next Steps / Critical Context ≤ 3 / Relevant Files*，要求"即使为空也要保留每个小节"，并附带一条保密规则（"NEVER include API keys, tokens, passwords, secrets"）。`_enforce_fifo_limits`（第 283 行）对返回文本确定性地重新施加条目数上限，并追加 `"(N earlier items omitted for brevity)"`。

## 🧱 静态回退（无 LLM 摘要）

`_build_static_fallback_summary`（第 198 行）在零模型调用的情况下产出同样的小节骨架：

- 最后一条用户请求 → *Latest Unresolved User Request*；第一条请求 → *Goal*；
- 含决策关键词（`decided`、`choosing`、`because`、`therefore`）的 AI 文本 → *Key Decisions*，否则归入 *Completed*；
- 每个工具调用 → *Completed*；形似路径的 token（包含 `/` 或 `\`，或以 `.py`/`.md`/… 结尾）→ *Relevant Files*（≤ 10）；
- 出错的 `ToolMessage` → *Blocked* 和 *Critical Context*。

当 `skip_llm` 生效时它被原样使用，并作为过短/失败 LLM 摘要的安全网。

## 🧹 非 LLM 策略

三者都在同一趟中运行（第 851 行），并且都尊重 `PROTECTED_TOOLS = {"memory", "skill_view", "skill_list"}`：

| 策略 | 模块 | 机制 |
| :------- | :----- | :-------- |
| **Dedup** | `tool_output_dedup.py` | 折叠重复且相同的工具输出 |
| **Prune** | `tool_output_prune.py` | 沿 `ToolMessage` **从新到旧**遍历，遇到摘要消息或 `status="compacted"` 结果即停止；跳过受保护工具；累加大小（chars // 4）：凡超出最新 `PRUNE_PROTECT_TOKENS (40 000)` token 的输出，其内容都被替换为 `[Old tool result content cleared]`。只有当总削减量 ≥ `PRUNE_MIN_REDUCTION_TOKENS (5 000)` 时才实际应用 |
| **Target truncate** | `target_truncation.py` | 把超大的输出向 `current_tokens × TARGET_TRUNCATE_RATIO (0.5)` 缩减：≥ `MIN_OUTPUT_CHARS_TO_TRUNCATE (500)` 字符的输出被裁到 `MAX_TOOL_OUTPUT_CHARS (2 000)` |

抢先式截断（管线之前，第 517 行）还会把每条不受保护的 `ToolMessage` 钳制在 2000 字符以内，保留头部 30%/尾部 30%，并加上 `...[omitted N chars]...` 标记。

## 📦 输出：摘要消息对

`_build_new_messages`（第 822 行）包装摘要文本，恰好产出两条消息：

```
[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted …
Respond ONLY to the latest user message that appears AFTER this summary.

<summary>
…summary Markdown…
</summary>

--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---
```

- **HumanMessage** `"What did we do so far?"`：一句中性提问，保持角色交替不被打断。
- **AIMessage** 携带 `additional_kwargs={"lc_source": "summarization"}`：这个标记被后续轮次用来 (a) 找到并链接上一个检查点，(b) 让 prune 在检查点处停下，(c) 让综合测试断言摘要一旦被取代，就能从模型视图中被吞掉。
- 总内容上限为 `SUMMARY_TOTAL_MAX_CHARS (16 000)` 字符，按头/尾 30/30 保留。

## 🛡️ 防抖动与退化恢复

状态保存在会话级的 `state_register_mem` 中，位于九个 `summarization_*` 键之下，每轮由 `before_agent` 重置（第 1159 行）。

**压缩调节器**（`_should_skip_compression` / `_record_compression`，第 613–662 行）：

| 护栏 | 阈值 | 效果 |
| :---- | :-------- | :----- |
| 总尝试次数 | `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5` | 本会话完全停止压缩 |
| 连续无效次数 | `INEFFECTIVE_THRESHOLD = 2` | 设置 `skip_llm`，只允许非 LLM 策略 |
| 有效性 | 消息条数减少**或** token 削减 ≥ `MIN_EFFECTIVENESS_PCT (0.05)` | 成功的非 LLM 策略会再次清除 `skip_llm` |

**退化监控器**（`_monitor_degradation`，第 988 行）：压缩之后，如果模型回复没有文本，计数器递增；当出现 `DEGRADATION_NO_TEXT_THRESHOLD (3)` 次连续空回复时，中间件强制恢复：计数器重置、`skip_llm` 清除、压缩重新启用，最多 `MAX_RECOVERY_ATTEMPTS (2)` 次。任何非空回复都会重置计数器。这条护栏专门捕捉"压缩 → 模型困惑 → 空输出 → 再压缩"的病态循环。

## 🔄 系统提示词刷新

仅限主 agent（`need_update_system_prompt=True`，第 1068–1074 行）：压缩之后，人设文件与长期记忆的相关性可能已经变化，因此中间件会从磁盘重新加载 `memory_store`，通过 `workspace.prompt_builder.build_system_prompt(session_id)` 重建系统提示词，把它同时写入 `state_register_mem` 与 `state_register_db` 的 `system_prompt` 键，并通过 `request.override(system_message=SystemMessage(...))` 注入。外层的 `ContextEngineHook` 会在后续调用中从注册表读取该值。

## 📌 注册位置

```python
# agent/core.py:152 — main agent (Summarization is the LAST middleware:
# innermost wrap layer, closest to the LLM)
Summarization(
    need_update_system_prompt=True,
    model=auxiliary_llm,
    main_llm_context_window=main_llm_max_tokens,
    trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
    keep=("messages", 10),
)

# agent/tools/subagent/spawn/core.py:755 — worker agent (first middleware)
Summarization(
    model=auxiliary_llm,
    main_llm_context_window=main_llm_max_tokens,
    trigger=[
        ("messages", 40),
        ("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO)),
    ],
    keep=("messages", 10),
)
```

## ⚙️ 配置参考

所有阈值都定义在 `config/num.py` 中。标记 ◆ 的值会被中间件导入；标记 ○ 的值虽已定义但**不被它消费**（见"诚实声明与局限"）。

| 常量 | 值 | 消费位置 |
| :------- | :---- | :------------- |
| `PREEMPTIVE_TRUNCATE_RATIO` ◆ | `0.70` | 抢先式闸门：仅截断阈值 |
| `COMPRESSION_TRIGGER_RATIO` ◆ | `0.80` | 抢先式闸门：完整压缩阈值；同时用于构建两处触发子句 |
| `MIN_PRESERVE_TOKENS` ◆ | `2_000` | 预算下限；无上下文窗口时的预算 |
| `MAX_PRESERVE_TOKENS` ◆ | `15_000` | 预算上限 |
| `PRESERVE_RATIO` ◆ | `0.25` | 预算 = 上下文窗口的 25% |
| `PRUNE_PROTECT_TOKENS` ◆ | `40_000` | prune：最新工具输出保留的 token 数 |
| `PRUNE_MIN_REDUCTION_TOKENS` ◆ | `5_000` | prune：实际生效所需的最小削减量 |
| `TARGET_TRUNCATE_RATIO` ◆ | `0.5` | target-truncate：向当前 token 数的 50% 缩减 |
| `MIN_OUTPUT_CHARS_TO_TRUNCATE` ◆ | `500` | target-truncate：资格线 |
| `MAX_TOOL_OUTPUT_CHARS` ◆ | `2_000` | target-truncate：单条输出上限 |
| `AGGRESSIVE_TRUNCATE_CHARS` ◆ | `1_000` | 激进兜底的硬切长度 |
| `SUMMARY_TOTAL_MAX_CHARS` ◆ | `16_000` | 摘要消息字符上限 |
| `CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO` ◆ | `0.3` / `0.3` | 所有头/尾保留比例 |
| `DEGRADATION_NO_TEXT_THRESHOLD` ◆ | `3` | 触发强制恢复前的空回复次数 |
| `MAX_RECOVERY_ATTEMPTS` ◆ | `2` | 强制恢复的预算 |
| `MAX_TOTAL_COMPRESSION_ATTEMPTS` ◆ | `5` | 调节器：会话总尝试上限 |
| `INEFFECTIVE_THRESHOLD` ◆ | `2` | 调节器：连续无效 → 跳过 LLM |
| `MIN_EFFECTIVENESS_PCT` ◆ | `0.05` | 调节器：token 削减有效性 |
| `PROTECTED_TOOLS` ◆ | `{"memory", "skill_view", "skill_list"}` | 免于一切缩减策略 |
| `LAST_TURN_RATIO_THRESHOLD` ◆ | `0.5` | 最后一轮压缩闸门 |
| `COMPLETED_MAX_ITEMS` / `KEY_DECISIONS_MAX_ITEMS` / `CRITICAL_CONTEXT_MAX_ITEMS` ◆ | `5` / `5` / `3` | FIFO 小节条数上限 |
| `FILE_OPS_LIST_MAX_CHARS` ◆ | `900` | 文件操作棘轮清单字符上限 |
| `LATEST_USER_REQUEST_MAX_CHARS` ◆ | `800` | 恢复上下文的请求字符上限 |
| `CHARS_PER_TOKEN`（估算器） | `4` | 确定性 token 估算的除数 |
| `SUMMARY_TRIM_TOKENS` ○ | `12_000` | 被中间件导入但从未读取 |
| `AUTO_CONTINUE_PROMPT` ○ | — | 被中间件导入但从未读取 |
| `DEGRADATION_MONITOR_COUNT` ○ | `5` | 已定义，未导入 |
| `COMPRESSION_RESERVE_TOKENS` ○ | `16_000` | 已定义，未导入 |
| `FILE_OPS_SECTION_MAX_CHARS` ○ | `2_000` | 已定义，未导入（只使用 900 字符的清单上限） |

## 🧪 测试

| 测试套件 | 覆盖内容 |
| :---- | :----- |
| `tests/module/test_summarization_comprehensive.py` | 140 个用例的模块级套件：触发闸门、预算/截断点、FIFO 上限、回退、prune/dedup/target-truncate、退化 |
| `tests/integration/test_interrupt_marker_approach.py` | 标记语义：摘要消息对能在后续压缩中存活；`AIMessage` 上的 `lc_source`；最后一轮压缩 |
| `tests/unit/test_pub_func_message_tools.py` | 估算器、prune（标记替换、保护窗口、最小削减门槛） |
| `tests/module/test_summarization_trigger.py` | 生产注册契约（未封顶窗口、0.80 阈值）+ 低 token 直通回归 |
| `tests/integration/` 密封 e2e | 全图静态回退压缩，零网络访问 |

完整的过程隔离套件（`uv run python tests/run_tests_split.py`）以 **2071 passed / 0 failed** 通过（GROUP A 1384P/2S + GROUP B 687P/5D）。

## ⚠️ 诚实声明与局限

- **`keep=("messages", 10)` 被接受但从未使用。** 构造函数为 API 兼容性存储了它；实际的尾部保留完全由预算决定（`PRESERVE_RATIO` × 上下文窗口，钳制在 [2000, 15000]）。修改 `keep` 没有任何效果。
- **文档逐字导入。** `json`、`hashlib`、`SUMMARY_TRIM_TOKENS` 与 `AUTO_CONTINUE_PROMPT` 在 `summarization.py` 顶部被导入但从未读取：它们是随本文件所依据的规范一并誊录的，并受 lint 门禁豁免覆盖。`DEGRADATION_MONITOR_COUNT`、`COMPRESSION_RESERVE_TOKENS` 与 `FILE_OPS_SECTION_MAX_CHARS` 在 `config/num.py` 中定义，但没有任何消费方。
- **估算器是 `chars // 4`，不是分词器。** 它有意保持确定性（测试可复现、预算稳定），并按英文/代码混合内容校准；CJK 占比高的内容会被低估（中文平均更接近每 token 1–2 字符，而不是 4）。
- **API 上报值胜过本地估算。** 当最后一条 `AIMessage` 携带 `usage_metadata.total_tokens` 时，触发判断以该数字（包含完整的 API 侧统计）为准：本地估算只是回退。
- **压缩是 fail-open 的。** `_apply_compression` 内部的任何异常都会被记录并吞掉；本轮以未压缩的历史继续。因此一个系统性损坏的辅助 LLM 只会导致非 LLM 缩减更频繁，而不会弄坏对话轮次。
- **静态回退是启发式的。** 基于关键词的决策/已完成分类与从原始工具参数中提取路径都是尽力而为：小节骨架有保证，内容质量没有。
- **`_SUMMARY_PREFIX`/`_SUMMARY_SUFFIX`/`<summary>` 标签/`lc_source="summarization"` 是承重的精确字符串。** 后续轮次的检查点链接（`_extract_previous_summary`）、prune 的停止条件以及各测试套件都按字面匹配它们：不要随意改写。
