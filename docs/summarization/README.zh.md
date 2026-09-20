# 🗜️ 上下文压缩：Summarization 中间件

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> Agent 如何让长对话保持在模型的上下文窗口之内：五个触发点覆盖整个生命周期（回合开始前、每次模型调用前、每次模型响应后、以及 provider 溢出报错时），一个纯函数式的四路路由选择最省钱的修复手段（先截断超大工具输出和超大的工具调用参数，实在不行才让 AI 压缩历史），防抖护栏保证压缩永远不会失控打转。

事实来源：`agent/middlewares/summarization/core.py`、`agent/middlewares/summarization/plan_context.py`、`pub/func/message/overflow_router.py`、`pub/func/message/tool_result_ttl.py`、`pub/func/message/llm_error_classifier.py`、`pub/func/estimate_tokens.py`、`pub/func/message/tool_output_dedup.py`、`pub/func/message/tool_output_prune.py`、`pub/func/message/target_truncation.py`、`pub/func/message/tool_args_truncate.py`、`pub/func/message/turn_utils.py`、`config/features/agent_side/summarization.py`，外加两处注册点 `agent/core.py` 和 `agent/tools/subagent/spawn/core.py`。本文档中的每一处行号与常量都已对照这些代码逐一核实。

## 目录

- [概览](#-概览)
- [触发：生命周期与溢出路由](triggers/README.zh.md)
  - [🧭 生命周期：五个触发点（T1–T5）](triggers/README.zh.md#-生命周期五个触发点t1t5)
  - [🚦 四路溢出路由决策](triggers/README.zh.md#-四路溢出路由决策)
- [Token 估算（无分词器）](#-token-估算无分词器)
- [压缩内部机制](internals/README.zh.md)
  - [✂️ 截断轨道：预算截断与 TTL 模块](internals/README.zh.md#-截断轨道预算截断与-ttl-模块)
  - [🔁 压缩轨道：`_apply_compression` 内部](internals/README.zh.md#-压缩轨道_apply_compression-内部)
  - [🖼️ 压缩期媒体卸载（内联媒体 → 引用）](internals/README.zh.md#-压缩期媒体卸载内联媒体--引用)
  - [📝 LLM 摘要：提示词、链式与回退](internals/README.zh.md#-llm-摘要提示词链式与回退)
  - [🧱 静态回退（无 LLM 摘要）](internals/README.zh.md#-静态回退无-llm-摘要)
  - [📦 输出：摘要消息对](internals/README.zh.md#-输出摘要消息对)
  - [🛡️ 防抖护栏矩阵与退化恢复](internals/README.zh.md#-防抖护栏矩阵与退化恢复)
  - [🔄 系统提示词刷新](internals/README.zh.md#-系统提示词刷新)
  - [📌 注册点](internals/README.zh.md#-注册点)
- [配置参考](#-配置参考)
- [测试](#-测试)
- [⚠️ 诚实与局限](#%EF%B8%8F-诚实与局限)

## 🎯 概览

`Summarization`（`agent/middlewares/summarization/core.py`，类定义在第 500 行）是一个**从零实现**的 `AgentMiddleware` —— 它**并不**继承 LangChain 内置的 `SummarizationMiddleware`。它只挂载 agent 生命周期的两个位置：

- `before_agent` / `abefore_agent`（第 1946 / 1950 行）—— **T1 预检**
- `wrap_model_call` / `awrap_model_call`（第 1960 / 2046 行）—— **T2 派发、T3 响应后复检、T4/T5 错误恢复环**

在中间件链中它位于**最内层 —— 离 LLM 最近**。压缩发生后，历史始终呈现如下形态：

```
HumanMessage("What did we do so far?")
AIMessage(<summary>, lc_source="summarization")
<recent turns preserved verbatim>
```

因为替换物是一个 Human/AI 消息对，模型永远不会看到两条连续的同角色消息，也就不需要配对修复。

共有两处注册：

| 注册点 | 触发条件 | LLM | `need_update_system_prompt` |
| :--- | :------ | :-- | :-------------------------- |
| 主 agent（`agent/core.py:152`） | `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `True` |
| Worker/子 agent（`agent/tools/subagent/spawn/core.py:755`） | `("messages", 40)` **或** `("tokens", int(main_llm_max_tokens * 0.80))` | `auxiliary_llm` | `False`（默认） |

两者都传入 `main_llm_context_window=main_llm_max_tokens`（来自 `MAIN_LLM_MAX_TOKEN`）和 `keep=("messages", 10)`。

## 🪙 Token 估算（无分词器）

`pub/func/estimate_tokens.py`（109 行）刻意不依赖分词器、完全确定，并提供三层降级：

- **T1——API 上报用量：** 当最后一条 `AIMessage` 带 `usage_metadata`（或调用方显式传入 `reported_tokens`）时，`estimate_messages_tokens` 原样返回该值——provider 的真实计数会短路全部本地估算；
- **T2——CJK 感知启发式：** `estimate_text_tokens` 把文本拆成 CJK 字符（`// CHARS_PER_TOKEN_CJK = 2`）与其余字符（`// CHARS_PER_TOKEN = 4`），复用 `pub.func.cjk.count_cjk` 做检测；
- **T3——遗留 `len // 4`：** 不是独立代码路径——当 `count_cjk(text) == 0` 时它就是 T2 的退化情形，因此纯 ASCII 估算与旧数字完全一致。

```python
tokens = (cjk chars // CHARS_PER_TOKEN_CJK)   # CHARS_PER_TOKEN_CJK = 2
       + (other chars // CHARS_PER_TOKEN)     # CHARS_PER_TOKEN = 4
# 消息级：str content → 文本估算
#        + Σ tool_call name/args 字符 + tool_call_id 字符
# 列表 content → 逐块：文本块按文本，媒体块按固定分型成本，
#   未知块按保守的未知成本
```

content 为**列表**时逐块计数，绝不再 JSON 序列化整份列表：文本块按文本估算，媒体块（`image_url` / `audio_url` / `video_url` / `audio_bytes` / `video_bytes`，或任何携带 `data:` 载荷的块）取 `TOKEN_ESTIMATION` 的固定成本 —— `tokens_per_image_block = 85`、`tokens_per_audio_block = 256`、`tokens_per_video_block = 1024` —— 未知块取 `tokens_per_unknown_block = 85`。被移除的 JSON 路径会把 5 MB base64 算成约 125 万 token，单张图片就会触发压缩；这些固定值是在没有模型时无法推导真实 token 数的媒体的保守替代。`str` content 与 `None` 不变，纯文本列表估算等同于拼接后的文本。

`pub/func/message/estimate_msg_tokens.py` 现为同一组 helper 的向后兼容 re-export。它快、跨运行稳定（相同输入 → 相同数字 → 测试可复现），并且有意做成保守近似。触发/预算路径上的任何环节都不依赖模型分词器。

## ⚙️ 配置参考

所有阈值集中在 `config/features/agent_side/summarization.py`（SUMMARIZATION TypedDict）。标 ◆ 的常量被存活代码路径消费；标 ○ 的常量虽有定义或导入、但**没有**被任何存活路径消费（见"诚实与局限"）。

| 常量 | 值 | 消费位置 |
| :------- | :---- | :------------- |
| `COMPRESSION_TRIGGER_RATIO` ◆ | `0.80` | `decide_route` 的硬溢出档；T3 压力闸门；构造两处触发子句 |
| `PREEMPTIVE_TRUNCATE_RATIO` ◆ | `0.70` | `decide_route` 的软溢出档 |
| `COMPRESSION_RESERVE_TOKENS` ◆ | `16_000` | `_usable_budget`（:615）：窗口 − 保留量 |
| `TRUNCATE_BUDGET_RATIO` ◆ | `0.60` | 截断轨道预算 = usable × 0.60（:680） |
| `MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE` ◆ | `200` | `find_truncatable_tool_results` 的候选门槛 |
| `TRUNCATABLE_RECENT_SKIP` ◆ | `6` | 最新若干条永不可截断（配对安全边距） |
| `MAX_OVERFLOW_RETRIES` ◆ | `3` | T4/T5 强制恢复上限（单一共用计数器） |
| `OVERFLOW_CLIP_ENABLED` ◆ | `True` | P1-2 无 LLM 尾部裁剪总开关 |
| `OVERFLOW_CLIP_MAX_REMOVE` ◆ | `10` | 单次裁剪最多 stub 的尾部消息数 |
| `OVERFLOW_CLIP_MIN_KEEP` ◆ | `5` | 转录下限：≤ 该条数 → 不裁剪 |
| `MAX_COMPRESS_ATTEMPTS_PER_TURN` ◆ | `3` | 每回合主动压缩上限 |
| `COMPACTION_COOLDOWN_ROUNDS` ◆ | `3` | 每次实际 compact 后武装的冷却期 |
| `MIN_PRESERVE_TOKENS` ◆ | `2_000` | 保留预算下限；无窗口时的预算 |
| `MAX_PRESERVE_TOKENS` ◆ | `15_000` | 保留预算上限 |
| `PRESERVE_RATIO` ◆ | `0.25` | 保留预算 = 窗口的 25% |
| `PRUNE_PROTECT_TOKENS` ◆ | `40_000` | 修剪：保留最新工具输出 token 数 |
| `PRUNE_MIN_REDUCTION_TOKENS` ◆ | `5_000` | 修剪：应用的最小收益 |
| `TARGET_TRUNCATE_RATIO` ◆ | `0.5` | 定向截断：向当前 token 的 50% 收缩 |
| `MIN_OUTPUT_CHARS_TO_TRUNCATE` ◆ | `500` | 定向截断：资格线 |
| `MAX_TOOL_OUTPUT_CHARS` ◆ | `2_000` | 定向截断：单输出上限 |
| `MIN_ARGS_CHARS_TO_TRUNCATE` ◆ | `500` | 工具参数截断：资格线（JSON 序列化后的参数长度） |
| `MAX_TOOL_ARGS_CHARS` ◆ | `2_000` | 工具参数截断：单参数上限 |
| `AGGRESSIVE_TRUNCATE_CHARS` ◆ | `1_000` | 激进兜底切割长度（工具输出与工具调用参数） |
| `SUMMARY_TOTAL_MAX_CHARS` ◆ | `16_000` | 摘要消息字符上限 |
| `CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO` ◆ | `0.3` / `0.3` | 所有头/尾保留（摘要与 TTL 截断） |
| `DEGRADATION_NO_TEXT_THRESHOLD` ◆ | `3` | 触发强制恢复前的空回复数 |
| `MAX_RECOVERY_ATTEMPTS` ◆ | `2` | 退化恢复预算 |
| `MAX_TOTAL_COMPRESSION_ATTEMPTS` ◆ | `5` | 控制器：会话尝试上限 |
| `INEFFECTIVE_THRESHOLD` ◆ | `2` | 控制器：连续无效 → 跳过 LLM |
| `MIN_EFFECTIVENESS_PCT` ◆ | `0.05` | 控制器：token 缩减有效性 |
| `PROTECTED_TOOLS` ◆ | `{"memory", "skill_view", "skill_list"}` | 豁免于一切收缩策略 |
| `LAST_TURN_RATIO_THRESHOLD` ◆ | `0.5` | 最后一回合压缩闸门 |
| `COMPLETED_MAX_ITEMS` / `KEY_DECISIONS_MAX_ITEMS` / `CRITICAL_CONTEXT_MAX_ITEMS` ◆ | `5` / `5` / `3` | FIFO 段落上限 |
| `ACTIVE_PLAN_NOTES_MAX_ITEMS` / `EVICTED_REFS_MAX_ITEMS` ◆ | `20` / `20` | 文档数组上限（计划注意事项 / 驱逐指针） |
| `FILE_OPS_LIST_MAX_CHARS` ◆ | `900` | 文件操作棘轮列表上限 |
| `LATEST_USER_REQUEST_MAX_CHARS` ◆ | `800` | 恢复上下文请求上限 |
| `CHARS_PER_TOKEN` / `CHARS_PER_TOKEN_CJK`（估算器） | `4` / `2` | 确定性 token 估算除数（非 CJK / CJK）；定义于 `config/features/agent_side/token_estimation.py` |
| `TOKENS_PER_IMAGE_BLOCK` / `TOKENS_PER_AUDIO_BLOCK` / `TOKENS_PER_VIDEO_BLOCK` / `TOKENS_PER_UNKNOWN_BLOCK`（估算器） | `85` / `256` / `1024` / `85` | 多模态 content 列表的逐块固定成本 —— base64 绝不计入文本；定义于 `config/features/agent_side/token_estimation.py` |
| `PRUNE_TTL_SECONDS` | `300` | TTL 过期地平线 —— 仅 TTL 三件套消费（如今仅测试） |
| `TTL_REGISTRY_MAX_ENTRIES` | `512` | TTL 首见注册表上限（如今仅测试） |
| `SUMMARY_TRIM_TOKENS` ○ | `12_000` | 被中间件导入、从未读取 |
| `AUTO_CONTINUE_PROMPT` ○ | — | 被中间件导入、从未读取 |
| `DEGRADATION_MONITOR_COUNT` ○ | `5` | 有定义、未被导入 |
| `FILE_OPS_SECTION_MAX_CHARS` ○ | `2_000` | 有定义、未被导入（实际使用的是 900 字符的列表上限） |

## 🧪 测试

| 套件 | 用例 | 覆盖 |
| :---- | :---- | :----- |
| `tests/pub/func/message/test_overflow_router.py` | 29 | `compute_pressure` / `find_truncatable_tool_results` / `decide_route` 各档位、候选规则、稳定路由字符串 |
| `tests/pub/func/message/test_tool_result_ttl.py` | 28 | 原地截断、配对不变量、非空占位符、注册表上限、预算截断 |
| `tests/pub/func/message/test_llm_error_classifier.py` | 56 | 413 状态码、文本提示、7 种溢出模式、cause 链深度、只读保证 |
| `tests/pub/func/message/test_pub_func_message_tools.py` | 29 | 去重 / 修剪 / 定向截断 / 回合工具，外加工具参数截断：头+尾格式、小参数跳过、释放量钳制、受保护工具、跳过最近消息、配对与无变异 |
| `tests/pub/func/message/test_read_file_slice.py` | 12 | read_file 可找回切片：原路径 + 1-based 续读 offset 通知、不跳行、页码绝对定位、通用标记逐字节一致、受保护 / 未超预算 / 回退路径 |
| `tests/config/test_num_contract.py` | 46 | 常量契约（看门狗 `CONTRACT_NAMES` 覆盖全部文档化旋钮） |
| `tests/pub/func/message/test_overflow_clip.py` | 21 | P1-2 纯裁剪：尾部批次检测、max_remove/min_keep/enabled 闸门、token 目标、标记保留（P0-2 指针、P2-4 通知）、no-op 幂等、配对不变量 |
| `tests/agent/middlewares/test_summarization_overflow_clip.py` | 9 | P1-2 中间件集成：T1/T2 不调 LLM 裁剪、裁剪不足降级、总开关、T4/T5 先裁后重试与裁→压缩降级、同步/异步奇偶、sanitizer 不移位 |
| `tests/agent/middlewares/test_compression_comprehensive.py` | 52 | 12 个类：T2 软溢出、T2 冷却期、T2 负面/无操作、同步/异步奇偶、T1 预检、路由决策、T3 触发/三形态/负面双跑、T4/T5 恢复、完整防抖矩阵、全分支奇偶、链式摘要过滤 |
| `tests/agent/middlewares/test_compression_media_offload.py` | 12 | 压缩期内联媒体卸载：写盘 + 指针、内容 hash 去重、解码/写盘失败占位、保留窗口媒体不动、同步/异步两路径、`evicted_refs` 收集 |
| `tests/pub/func/test_estimate_tokens_media.py` | 22 | 逐块多模态估算：固定分型成本、5 MB base64 回归、未知块藏媒体、`str` / `None` / 空列表 / 纯文本边界 |
| `tests/agent/middlewares/test_summary_message_filtering.py` | 6 | 链式摘要过滤：旧消息对从序列化对话中移除、普通/空/多对输入、无标记的旧会话 human 保留、async `_acreate_summary` 镜像 |
| `tests/agent/middlewares/test_summary_doc.py` + `test_summary_doc_middleware.py` | 41 | 结构化摘要：schema 兼容/强制转换、渲染往返 + 字节稳定 + 节顺序、代码层 cap + 注记、latest request 逐字、json_mode/json_repair/free-form 三档、prior-doc JSON 链式、旧 MD 过渡、驱逐指针收集与延续 |
| `tests/agent/middlewares/test_summary_active_plan.py` | 20 | Part 1：计划活跃判定（state/todo 来源、全完成门、fail-open）、首摘/更新两条提示词路径注入、跨链式压缩的注意事项继承/追加/cap/清空、latest request 逐字 + 驱逐指针、多消息连发 |
| `tests/agent/middlewares/test_compression_e2e_static.py` | 18 | 6 个端到端场景 + 3 个溢出计数器回归测试 × 2 种注册顺序、静态回退压缩、零网络 |
| `tests/agent/middlewares/test_summarization_trigger.py` | 3 | 注册契约（测试固定窗口）：`MAIN_LLM_MAX_TOKEN = 65 536` → 触发阈值 `52 428`；低 token 直通 |
| `tests/agent/middlewares/test_summarization_comprehensive.py` | 140 | 遗留深度套件：切点/预算、FIFO 上限、回退、修剪/去重/定向截断、退化 |
| `tests/agent/middlewares/test_e2e_summarization.py` | 7 | 全图封闭式 e2e：真实 `create_agent` 链（主模型为捕获桩、辅助模型为失败桩）驱动静态回退摘要路径；零网络，窗口 32 000（按比例缩小），缺少 MAIN_LLM 配置时跳过 |
| `tests/agent/middlewares/message_persistence/` | 31 | 边界 + 工具返回增量落库：每条消息恰好一次、跨边界不重复、重启重放靠持久水位不增行、同步 + 异步钩子、缺 session_id 跳过、HITL 拒绝配对重挂、过滤语义；另有 T1/T2/T3 压缩路径零写库证明，以及摘要对（两条）零写库证明 |
| `tests/agent/middlewares/test_compression_nudges.py` | 2 | 压缩时 nudge 派发：memory review + plan extraction 从 compact 路径触发；无切点压缩不派发 |
| `tests/context_engine/store/test_persisted_message_ids.py` | 3 | 持久水位存储：幂等标记、会话隔离、会话删除时清理、空输入无操作 |
| `tests/context_engine/store/test_interrupt_marker_approach.py` | 11 | 标记语义：摘要消息对在后续压缩中存活；FACT C 固定装置（窗口 26 000 → usable 10 000，截断线 7 000） |

全量进程隔离套件（`uv run python tests/run_tests_split.py`）通过：**4268 passed / 12 skipped / 0 failed**（GROUP A 3282P/1S + GROUP B 913P/11S + GROUP C 73P）。

## ⚠️ 诚实与局限

- **`keep=("messages", 10)` 被接受但从未使用。** 构造函数仅为 API 兼容而存储它；尾部保留由预算决定（`PRESERVE_RATIO` × 窗口，夹在 [2 000, 15 000]），加上路由的 `TRUNCATABLE_RECENT_SKIP` 边距。改 `keep` 没有任何效果。
- **纯装饰性导入。** `summarization/core.py` 顶部的 `json`、`hashlib`、`SUMMARY_TRIM_TOKENS` 与 `AUTO_CONTINUE_PROMPT` 被导入但从未读取；`DEGRADATION_MONITOR_COUNT` 与 `FILE_OPS_SECTION_MAX_CHARS` 在 `config/features/agent_side/summarization.py` 的 `SUMMARIZATION` TypedDict 中有定义但无人消费。
- **TTL 注册表没有接入生产。** `record_first_seen` / `select_expired` / `truncate_expired`（以及 `PRUNE_TTL_SECONDS`、`TTL_REGISTRY_MAX_ENTRIES`）只有测试在用；中间件只使用 `truncate_to_budget`。对 `agent/` 的 grep 找不到 TTL 三件套的任何生产调用点。注册表同样是易失的（内存态、以 `tool_call_id` 为键、重启即失）。
- **保留但失效的代码。** `_preemptive_check`（:589）与 `_preemptive_truncate`（:1159）仅供参照：没有任何生产调用点会走到它们实现的二档抢先机制。
- **估算器是三层、不依赖分词器的启发式，而不是分词器。** 有 API 上报用量时 T1 直接返回；T2 是 CJK 感知启发式（CJK 字符按 `CHARS_PER_TOKEN_CJK = 2`、其余按 `CHARS_PER_TOKEN = 4`）；T3 是遗留的 `len // 4`，即 T2 的纯 ASCII 退化情形。它刻意保持确定性（测试可复现、预算稳定）；`CHARS_PER_TOKEN_CJK = 2` 对应中文平均更接近 1–2 字符/token 而非 4 的事实。
- **上报值何时胜出。** T3 是唯一由上报用量驱动的触发点（`compute_pressure` 取 max）。T1/T2 的路由决策由估算驱动（仅估算 + 系统提示词开销）；遗留的 `_check_trigger` 子句兜底使用 `max(本地估算, 上报值)`。
- **T3 绝不改写返回的响应。** T3 派发的持久效果是工具输出的原地截断（消息对象与图状态共享）和防抖记账；T3 的 compact 路由 `request.override` 只在本地生效，原始响应始终返回。整个 T3 函数体 fail-open。
- **T4/T5 设计上绕过防抖矩阵** —— 这正是"强制"的意义所在。超过 `MAX_OVERFLOW_RETRIES (3)`（T4/T5 共用的单一计数器，每回合重置）、或强制压缩步骤自身失败时，原始 provider 异常向上传播（绝不吞掉、绝不被压缩错误顶替）。
- **压缩是 fail-open 的。** `_apply_compression` 内的任何异常都会记日志并吞掉；回合带着未压缩的历史继续。
- **静态回退是启发式的。** 基于关键词的决策/完成分类与从原始工具参数提取路径都是尽力而为；段落骨架有保证，内容质量没有。
- **结构化档使用 `json_mode` 是因为当前端点要求如此。** `glm-5.3-flash`（openai 兼容）不会为 `with_structured_output` 发出函数调用，默认 function-calling 档会抛 Pydantic 解析错误；`_structured_runnable` 对不接受 `method` 参数的 provider 退回无参调用，其余由 `json_repair` 与 free-form 两档兜底。代码层 cap 与渲染器仍保证输出形态。
- **`_SUMMARY_PREFIX`/`_SUMMARY_SUFFIX`/`<summary>` 标签/`lc_source="summarization"` 是承重的精确字符串。** 后续回合的链式（`_extract_previous_summary`）、修剪停止条件与全部测试套件都按字面匹配它们 —— 不要随手改写。
