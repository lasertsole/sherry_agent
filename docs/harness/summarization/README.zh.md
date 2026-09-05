# 🗜️ 上下文压缩：Summarization 中间件

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> Agent 如何让长对话保持在模型的上下文窗口之内：五个触发点覆盖整个生命周期（回合开始前、每次模型调用前、每次模型响应后、以及 provider 溢出报错时），一个纯函数式的四路路由选择最省钱的修复手段（先截断超大工具输出，实在不行才让 AI 压缩历史），防抖护栏保证压缩永远不会失控打转。

事实来源：`agent/middlewares/summarization.py`、`pub_func/message/overflow_router.py`、`pub_func/message/tool_result_ttl.py`、`pub_func/message/llm_error_classifier.py`、`pub_func/message/estimate_msg_tokens.py`、`pub_func/message/tool_output_dedup.py`、`pub_func/message/tool_output_prune.py`、`pub_func/message/target_truncation.py`、`pub_func/message/turn_utils.py`、`config/num.py`，外加两处注册点 `agent/core.py` 和 `agent/tools/subagent/spawn/core.py`。本文档中的每一处行号与常量都已对照这些代码逐一核实。

## 目录

- [概览](#-概览)
- [生命周期：五个触发点（T1–T5）](#-生命周期五个触发点t1t5)
- [四路溢出路由决策](#-四路溢出路由决策)
- [Token 估算（无分词器）](#-token-估算无分词器)
- [截断轨道：预算截断与 TTL 模块](#-截断轨道预算截断与-ttl-模块)
- [压缩轨道：`_apply_compression` 内部](#-压缩轨道_apply_compression-内部)
- [LLM 摘要：提示词、链式与回退](#-llm-摘要提示词链式与回退)
- [静态回退（无 LLM 摘要）](#-静态回退无-llm-摘要)
- [输出：摘要消息对](#-输出摘要消息对)
- [防抖护栏矩阵与退化恢复](#-防抖护栏矩阵与退化恢复)
- [系统提示词刷新](#-系统提示词刷新)
- [注册点](#-注册点)
- [配置参考](#-配置参考)
- [测试](#-测试)
- [⚠️ 诚实与局限](#%EF%B8%8F-诚实与局限)

## 🎯 概览

`Summarization`（`agent/middlewares/summarization.py`，类定义在第 490 行）是一个**从零实现**的 `AgentMiddleware` —— 它**并不**继承 LangChain 内置的 `SummarizationMiddleware`。它只挂载 agent 生命周期的两个位置：

- `before_agent` / `abefore_agent`（第 1894 / 1898 行）—— **T1 预检**
- `wrap_model_call` / `awrap_model_call`（第 1908 / 1994 行）—— **T2 派发、T3 响应后复检、T4/T5 错误恢复环**

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

## 🧭 生命周期：五个触发点（T1–T5）

```
回合开始
│
├─ T1  before_agent 预检  (_t1_preflight :1834 / _at1_preflight :1865)
│      ├─ _reset_turn_state (:1797) 重置 10 个每回合计数器
│      ├─ _decide_overflow_route (:622) → None / "fits" → 直接放行
│      ├─ 冷却期 > 0 时封锁 COMPACT 路由；截断轨道仍然运行
│      │  （它本身就是最廉价的恢复机制）
│      └─ 派发（trigger="T1"）+ _t1_state_update (:1810) 把结果提交进图：
│         [RemoveMessage(id=REMOVE_ALL_MESSAGES), *new_messages]
│         （add_messages reducer 自己从不删除消息 —— RemoveMessage
│         哨兵是被压缩掉的前缀真正离开状态的唯一途径）
│
├─ T2  wrap_model_call，handler 之前（:1908 同步 / :1994 异步）
│      ├─ 先读 force 标志（:1921）再过跳过闸门 —— 跳过闸门
│      │  （_should_skip_compression :1234）会消费该标志
│      ├─ _tick_cooldown（:811）：每次调用都递减冷却计数
│      ├─ 防抖闸门（:1931–1934）：
│      │    if not forced and (cooldown_active or
│      │               attempts >= MAX_COMPRESS_ATTEMPTS_PER_TURN):
│      │      直接放行（若刚发生过 compact 则重建系统提示词，
│      │      :1938–1952）→ handler → 监控 → T3
│      ├─ 否则：四路决策（:1967）→ _dispatch_overflow_route；
│      │  若遗留触发子句命中（_check_trigger :566，例如
│      │  ("messages", 40)）→ ROUTE_COMPACT_ONLY（:1972）
│      └─ 三处 handler 调用点（:1927、:1953、:1978）全部运行在
│         _execute_with_recovery（:1036）之内 —— 即 T4/T5 恢复环
│
├─ T3  响应后复检  (_post_response_check :828 / 异步 :901)
│      ├─ 若 T2 在本次 wrap 调用中已压缩则跳过（t2_compressed
│      │  标志，:1980–1983）—— 每次模型调用至多一次压缩
│      ├─ extract_reported_input_tokens(response)（:127）；None → 返回
│      ├─ 闸门：回合尝试上限、冷却期、可用预算
│      ├─ pressure = max(估算 + 系统提示词, 上报值) —— provider
│      │  上报的输入 token 数优先（compute_pressure）
│      ├─ pressure < usable × 0.80 → 返回；路由为 "fits" → 返回
│      └─ 派发（trigger="T3"）并始终返回原始响应；整个函数体
│         fail-open（任何异常 → 记日志，原始响应原样保留）
│
└─ T4/T5  provider 报错恢复环
       (_execute_with_recovery :1036 / _aexecute_with_recovery :1094)
       ├─ handler 抛异常 → classify_provider_error
       │  （pub_func/message/llm_error_classifier.py）：
       │  payload_too_large → T4，context_overflow → T5
       │  （_TRIGGER_BY_ERROR_CLASS :112，_RETRY_KEY_BY_ERROR_CLASS :116）
       ├─ 非目标类 / 未知类 → 原始异常原样重新抛出（零重试、
       │  零状态写入、绝不吞掉）
       ├─ 重试次数 < MAX_OVERFLOW_RETRIES (3) → _forced_recovery_request
       │  （:964 / 异步 :1009）：强制压缩 + 预算截断，构造上绕过
       │  全部防抖闸门（冷却期、每回合上限与 _should_skip_compression
       │  均不被咨询）；它不武装冷却期、不计回合尝试，但会经过
       │  _record_compression 保证会话统计真实；每类重试计数器在
       │  成功之后才递增（:1000）
       ├─ 重试耗尽 → 原始异常重新抛出（错误帧经 messages.py →
       │  turn_runner.py 链路向上传播 —— 绝不返回空响应）
       └─ 强制压缩步骤自身失败 → 原始异常重新抛出
          （raise exc from compression_exc）。_monitor_degradation
          只在恢复环返回之后的最终成功响应上运行一次。
```

遗留触发子句仍然作为 T2 的兜底存在（`_check_trigger`，:566）：`("messages", N)` 按历史长度触发，`("tokens", N)` 按 `max(本地估算, 最后一条 AIMessage 上报的 usage_metadata.total_tokens)` ≥ N 触发。子句列表是 OR 关系。

## 🚦 四路溢出路由决策

`pub_func/message/overflow_router.py` 是一个**纯决策层** —— 不截断、不压缩、无 I/O、无状态。中间件从它导入三个函数：

- `compute_pressure`（:50）= `max(estimated_tokens + system_prompt_tokens, reported_tokens)` —— 有 API 上报值时以上报值为准；
- `find_truncatable_tool_results`（:68）—— **只有** `ToolMessage` 有资格（工具输出可再生）；最近 `TRUNCATABLE_RECENT_SKIP (6)` 条消息永远排除在外，保证最新的 tool/ai 配对完好；候选必须值回票价：估算 token ≥ `MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE (200)`；结果按 token 数降序排列，执行器先切最大的赢家；
- `decide_route`（:103）—— 派发契约（字符串稳定）：

| 压力（`p`）与 `usable` 的关系 | 无截断候选 | 有截断候选 | 候选 token 总和 vs 溢出量（`p − usable`） |
| :------------------------- | :------------------------ | :--------------- | :--------------------------------------------- |
| `p < 0.70 × usable` | `fits` | `fits` | — |
| 软溢出 `0.70 × usable ≤ p < 0.80 × usable` | `fits` | `truncate_tool_results_only` | —（软溢出**绝不会**单独触发压缩） |
| 硬溢出 `p ≥ 0.80 × usable` | `compact_only` | 总和 ≥ 溢出量 → `truncate_tool_results_only`；总和 < 溢出量 → `compact_then_truncate` | 溢出量 = `p − usable` |

所有阈值输入都从**可用预算**推导，而不是原始窗口：

```
usable_budget  = max(context_window − COMPRESSION_RESERVE_TOKENS(16_000), 0)   # _usable_budget :605
system_est     = len(state_register_mem["system_prompt"]) // 4                  # :616–620
truncate line  = usable × PREEMPTIVE_TRUNCATE_RATIO (0.70)
compact line   = usable × COMPRESSION_TRIGGER_RATIO (0.80)
truncate budget= usable × TRUNCATE_BUDGET_RATIO (0.60)
```

唯一的执行器 `_dispatch_overflow_route`（:739 同步 / :779 异步）同时服务 T1、T2 **和** T3 —— 绝不复制第二份：

- `truncate_tool_results_only` → `_run_budget_truncation`（:649）原地截断，然后**复检**：如果释放的 token 不够（`new_tokens ≥ usable × 0.80`），升级为 `compact_then_truncate`；否则直接放行、不做压缩；
- `compact_only` / `compact_then_truncate` → `_execute_compact`（:681 / 异步 :710）→ `_apply_compression`（异常记日志、请求原样返回）→ `_record_compaction_bookkeeping`（:673：武装冷却期、计一次回合尝试）→ `compact_then_truncate` 还会对压缩结果再跑一次预算截断兜底 → 按新旧 token 与压力比记录路由日志。

窗口算术（测试契约）：窗口 `41 600` → usable `25 600`，两条线 `17 920` / `20 480`，截断预算 `15 360`。当 `MAIN_LLM_MAX_TOKEN = 65536` 时，注册的 T2 子句落在 `52 428`。

## 🪙 Token 估算（无分词器）

`pub_func/message/estimate_msg_tokens.py`（29 行）刻意不依赖分词器、完全确定：

```python
tokens = (content chars            # str content, or len(json.dumps(content))
        + Σ tool_call name/args chars
        + tool_call_id chars) // CHARS_PER_TOKEN   # CHARS_PER_TOKEN = 4
```

它快、跨运行稳定（相同输入 → 相同数字 → 测试可复现），并且有意做成保守近似。触发/预算路径上的任何环节都不依赖模型分词器。

## ✂️ 截断轨道：预算截断与 TTL 模块

`pub_func/message/tool_result_ttl.py` 提供截断轨道使用的原地截断。设计不变量（承重）：

- **只原地修改** —— 该模块从不删除、重排或弹出消息；只修改 `msg.content`（或 content 列表块）并返回索引。这保住了 provider API 与 `ToolCallNormalize` 依赖的 tool-call/`ToolMessage` 配对。
- **占位符非空** —— 被截断的结果始终保留非空内容：`ToolCallNormalize.before_model` 会**丢弃空的 `ToolMessage`** 来净化转录，空占位符会悄悄破坏配对。
- **头部 30% / 尾部 30% 保留**（`CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO`）加省略标记。

中间件实际消费的部分：**只有 `truncate_to_budget`**，由路由的候选列表驱动 —— `_run_budget_truncation`（:649）按预算（`usable × TRUNCATE_BUDGET_RATIO`）截断候选。

TTL 注册表本体（`record_first_seen` / `select_expired` / `truncate_expired`、`PRUNE_TTL_SECONDS = 300`、`TTL_REGISTRY_MAX_ENTRIES = 512`、以 `tool_call_id` 为键、重启即失）如今**只有测试套件在用** —— 中间件没有接入任何按龄过期的逻辑（见"诚实与局限"）。

## 🔁 压缩轨道：`_apply_compression` 内部

`_apply_compression`（:1636；异步孪生 :1708）按顺序执行：

1. **捕获恢复上下文**（`_capture_recovery_context`，:1525）：最后一条用户请求（≤ 800 字符）与文件操作棘轮 —— 从 `read`/`write` 族工具调用中提取路径（:405），与上一轮的集合合并（读过的会记住，改过的文件绝不会被降级为只读）。
2. **非 LLM 策略**（`_run_non_llm_strategies`，:1472）：`去重 → 修剪 → 定向截断`（细节见下）。这些是免费的 —— 不调模型。
3. **是否用 LLM 的决策**：

   ```
   if tokens_after_non_llm > budget × 2  OR  skip_llm  OR  nothing was reduced:
       summarize [0:cutoff] and rebuild   → strategy "llm_summary" / "fallback"
   else:
       keep as-is                          → strategy "non_llm_sufficient"
   ```

   非 LLM 收缩先拿走第一次机会；只有当历史仍超过保留预算的两倍（或防抖控制器已禁用 LLM 摘要，或非 LLM 策略毫无所得）时才花费辅助 LLM。
4. **激进兜底**（`_aggressive_truncate`，:1508）：如果结果*依然*过大，每个超过 `AGGRESSIVE_TRUNCATE_CHARS (1 000)` 字符的 `ToolMessage` 被硬切并加标记。
5. **摘要自截断**（`_truncate_summary_messages`，:1578）：任何超过 `SUMMARY_TOTAL_MAX_CHARS (16 000)` 字符的既有摘要消息（`lc_source == "summarization"`）被重新截断为头部 30% / 尾部 30%（`_truncate_content`，:1570）。
6. **恢复注入**（`_inject_recovery_context`，:1543）：捕获的文件操作棘轮被改写进摘要的 `## Relevant Files` 段，检查点始终携带最新的读/改文件地图。
7. **记账**（`_record_compression`，:1256），最后 `request.override(messages=..., system_message=...)`。

**切点选择**（`_determine_cutoff`，:1289）：把历史切成回合，**从最新往回**累加、对照保留预算 `clamp(window × 0.25, 2 000, 15 000)`（`_calculate_preserve_budget`，:555）；放不下的整回合可以从中劈开。`_adjust_for_orphan_pairs`（:1319）再把切点往回走，直到没有 `ToolMessage` 与它的 `AIMessage` 工具调用分离。除非最后一回合比例闸门触发（最后一条用户消息 ≥ token 总量的 `LAST_TURN_RATIO_THRESHOLD (0.5)` —— `_check_last_turn_ratio`，在 wrap 入口 :1916/:2002 调用），切点绝不越过最后一条 `HumanMessage`。

所有失败模式都是 fail-open：`_apply_compression` 抛异常只会记日志，原始请求原样继续 —— 坏掉的压缩从不弄坏回合。

## 📝 LLM 摘要：提示词、链式与回退

`_create_summary` / `_acreate_summary`（:1389 / :1414）：

1. **序列化**（`_serialize_for_summary`，:255）：每条消息变成一行带标签的文本 —— `[User]:`（≤ 2 000 字符）、`[Assistant]:`（≤ 2 000 字符）、`[Assistant tool call]: name(args ≤ 500 chars)`、`[Tool result|Tool error] (id):`（> 2 000 字符 → 保留 1 800 + 省略标记）。
2. **链上之前的检查点**（`_extract_previous_summary`，:1355）：找到最新的 `additional_kwargs["lc_source"] == "summarization"` 的 `AIMessage`，抽取其 `<summary>…</summary>` 正文。若存在，提示词变为 `conversation + prior-summary + _SUMMARY_PROMPT_UPDATE`（:242）而非 `_SUMMARY_PROMPT_FIRST`（:234）—— 目标/约束/决策向前携带，冲突时最新优先，遵守 FIFO 上限。
3. **调用**辅助模型，带 `config={"metadata": {"lc_source": "summarization"}}`，让下游工具链能识别摘要调用。
4. **护栏：**响应为空或过短时回退到确定性摘要；任何异常同样回退。失败时 LLM 永远没有最终话语权。

提示词模板（`_SUMMARY_TEMPLATE`，:187）固定了 Markdown 骨架 —— *Latest Unresolved User Request / Goal / Constraints & Preferences / Progress（Completed ≤ 5 · In Progress · Blocked）/ Key Decisions ≤ 5 / Next Steps / Critical Context ≤ 3 / Relevant Files* —— 要求"即使为空也保留每一节"并带保密规则（"NEVER include API keys, tokens, passwords, secrets"）。`_enforce_fifo_limits`（:371）对返回文本确定性地重新施加条目上限，追加 `"(N earlier items omitted for brevity)"`。

## 🧱 静态回退（无 LLM 摘要）

`_build_static_fallback_summary`（:286）零模型调用产出同样的段落骨架：

- 最后一条用户请求 → *Latest Unresolved User Request*；第一条请求 → *Goal*；
- 含决策关键词（`decided`、`choosing`、`because`、`therefore`）的 AI 文本 → *Key Decisions*，否则 *Completed*；
- 每个工具调用 → *Completed*；路径样 token（含 `/` 或 `\`，或以 `.py`/`.md`/`.js`/`.ts`/`.json` 结尾）→ *Relevant Files*（≤ 10，排除 `http` 链接）；
- 报错的 `ToolMessage` → *Blocked* 与 *Critical Context*。

`skip_llm` 生效时原样使用它；它也是短/失败 LLM 摘要的安全网。

## 📦 输出：摘要消息对

`_build_new_messages`（:1443）包裹摘要文本，恰好产出两条消息：

```
[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted …
Respond ONLY to the latest user message that appears AFTER this summary.

<summary>
…summary Markdown…
</summary>

--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---
```

- **HumanMessage** `"What did we do so far?"` —— 一个中性问题，维持角色交替。
- **AIMessage**，带 `additional_kwargs={"lc_source": "summarization"}` —— 这个标记被后续回合用于：(a) 找到并链起之前的检查点，(b) 让修剪停在检查点处，(c) 让测试断言被取代后的摘要可以从模型视图整体吞下。
- 总内容以 `SUMMARY_TOTAL_MAX_CHARS (16 000)` 封顶，头部/尾部 30/30 保留。

## 🛡️ 防抖护栏矩阵与退化恢复

状态存放在会话级 `state_register_mem` 的**十四个** `summarization_*` 键中（:89–104）。`_reset_turn_state`（:1797）在每个回合开始时重置其中**十个**；`summarization_last_user_question`、`summarization_cooldown_rounds` 与两个 T4/T5 重试计数器被刻意**不**按回合重置。

| 护栏 | 键 | 阈值 | 效果 |
| :---- | :-- | :-------- | :----- |
| 回合冷却期 | `summarization_cooldown_rounds` | `COMPACTION_COOLDOWN_ROUNDS = 3` | 每次实际 compact 后武装（:673）；**每次**模型调用递减（:811）；封锁 T1 compact 路由、T2 主动压缩与 T3 —— 永不封锁 T4/T5 强制恢复环 |
| 每回合压缩数 | `summarization_turn_attempts` | `MAX_COMPRESS_ATTEMPTS_PER_TURN = 3` | 由 :673 递增；压制 T2 主动压缩 + T3（强制环豁免） |
| 每类溢出重试 | `summarization_overflow_retries_t4` / `_t5` | `MAX_OVERFLOW_RETRIES = 3` | 每次成功的强制步骤后递增；耗尽 → 原始 provider 错误向上传播 |
| 会话压缩总数 | `summarization_compression_count` | `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5` | `_should_skip_compression`（:1234）返回 True —— 主动压缩完全停止 |
| 连续无效次数 | `summarization_compression_ineffective` | `INEFFECTIVE_THRESHOLD = 2` | 置 `skip_llm` —— 只跑非 LLM 策略 |
| 有效性判定 | （`_record_compression`，:1256） | 消息数下降**或** token 缩减 ≥ `MIN_EFFECTIVENESS_PCT (0.05)` | 成功的非 LLM 策略（`dedup`/`prune`/`truncate`/`fallback`/`aggressive`）会再次清掉 `skip_llm` |
| 退化恢复预算 | `summarization_recovery_attempts` | `MAX_RECOVERY_ATTEMPTS = 2` | 限制退化监视器发起的强制恢复次数 |

**退化监视器**（`_monitor_degradation`，:1609）：只在本次调用真的发生过压缩时才被咨询（`_compaction_just_happened` 标志）。模型回复没有文本时计数器递增；连续 `DEGRADATION_NO_TEXT_THRESHOLD (3)` 次空回复 —— 且 `summarization_recovery_attempts < 2` —— 时置 `force_recovery`、清零无效连击与会话压缩计数。任何非空回复都会清零计数器。它捕捉的是"压缩 → 模型懵了 → 空输出 → 再压缩"的病态循环。注意二者的配合：force 标志在 wrap 入口（:1921）被读取，**先于** `_should_skip_compression`，而跳过闸门会消费它（重置各计数器并继续，:1235–1240）—— 恢复压缩恰好跑一次。

## 🔄 系统提示词刷新

仅主 agent（`need_update_system_prompt=True`）：压缩后中间件重建系统提示词并写入 `system_prompt` 状态键，让下一次模型调用看到的是当前的人设文件 / 长期记忆。两条送达路径：压缩后直接 `request.override(system_message=SystemMessage(...))`；以及当 T1 已发生过 compact、而防抖闸门又拦下了第二次压缩时，重建的提示词仍会在闸门路径中被注入（:1938–1952），因为不带 `ContextEngineHook` 的链路依赖本中间件送达它。

## 📌 注册点

```python
# agent/core.py:152 — 主 agent（Summarization 是最后一个中间件：
# 最内层 wrap，离 LLM 最近）
Summarization(
    need_update_system_prompt=True,
    model=auxiliary_llm,
    main_llm_context_window=main_llm_max_tokens,
    trigger=[("tokens", int(main_llm_max_tokens * COMPRESSION_TRIGGER_RATIO))],
    keep=("messages", 10),
)

# agent/tools/subagent/spawn/core.py:755 — worker agent（第一个中间件）
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

所有阈值集中在 `config/num.py`。标 ◆ 的常量被存活代码路径消费；标 ○ 的常量虽有定义或导入、但**没有**被任何存活路径消费（见"诚实与局限"）。

| 常量 | 值 | 消费位置 |
| :------- | :---- | :------------- |
| `COMPRESSION_TRIGGER_RATIO` ◆ | `0.80` | `decide_route` 的硬溢出档；T3 压力闸门；构造两处触发子句 |
| `PREEMPTIVE_TRUNCATE_RATIO` ◆ | `0.70` | `decide_route` 的软溢出档（旧的 `_preemptive_check` 两档闸门已退役） |
| `COMPRESSION_RESERVE_TOKENS` ◆ | `16_000` | `_usable_budget`（:605）：窗口 − 保留量 |
| `TRUNCATE_BUDGET_RATIO` ◆ | `0.60` | 截断轨道预算 = usable × 0.60（:660） |
| `MIN_TOOL_RESULT_TOKENS_TO_TRUNCATE` ◆ | `200` | `find_truncatable_tool_results` 的候选门槛 |
| `TRUNCATABLE_RECENT_SKIP` ◆ | `6` | 最新若干条永不可截断（配对安全边距） |
| `MAX_OVERFLOW_RETRIES` ◆ | `3` | 每错误类的 T4/T5 强制恢复上限 |
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
| `AGGRESSIVE_TRUNCATE_CHARS` ◆ | `1_000` | 激进兜底切割长度 |
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
| `FILE_OPS_LIST_MAX_CHARS` ◆ | `900` | 文件操作棘轮列表上限 |
| `LATEST_USER_REQUEST_MAX_CHARS` ◆ | `800` | 恢复上下文请求上限 |
| `CHARS_PER_TOKEN`（估算器） | `4` | 确定性 token 估算除数 |
| `PRUNE_TTL_SECONDS` | `300` | TTL 过期地平线 —— 仅 TTL 三件套消费（如今仅测试） |
| `TTL_REGISTRY_MAX_ENTRIES` | `512` | TTL 首见注册表上限（如今仅测试） |
| `SUMMARY_TRIM_TOKENS` ○ | `12_000` | 被中间件导入、从未读取 |
| `AUTO_CONTINUE_PROMPT` ○ | — | 被中间件导入、从未读取 |
| `DEGRADATION_MONITOR_COUNT` ○ | `5` | 有定义、未被导入 |
| `FILE_OPS_SECTION_MAX_CHARS` ○ | `2_000` | 有定义、未被导入（实际使用的是 900 字符的列表上限） |

## 🧪 测试

| 套件 | 用例 | 覆盖 |
| :---- | :---- | :----- |
| `tests/unit/test_overflow_router.py` | 29 | `compute_pressure` / `find_truncatable_tool_results` / `decide_route` 各档位、候选规则、稳定路由字符串 |
| `tests/unit/test_tool_result_ttl.py` | 28 | 原地截断、配对不变量、非空占位符、注册表上限、预算截断 |
| `tests/unit/test_llm_error_classifier.py` | 20 | 413 状态码、文本提示、7 种溢出模式、cause 链深度、只读保证 |
| `tests/unit/test_config_num.py` | 43 | 常量契约（看门狗 `CONTRACT_NAMES` 覆盖全部文档化旋钮） |
| `tests/module/test_compression_comprehensive.py` | 48 | 12 个类：T2 软溢出、T2 冷却期、T2 负面/无操作、同步/异步奇偶、T1 预检、路由决策、T3 触发/三形态/负面双跑、T4/T5 恢复、完整防抖矩阵、全分支奇偶 |
| `tests/module/test_compression_e2e_static.py` | 12 | 6 个端到端场景 × 2 种注册顺序、静态回退压缩、零网络 |
| `tests/module/test_summarization_trigger.py` | 3 | 生产注册契约：`MAIN_LLM_MAX_TOKEN = 65 536` → 触发阈值 `52 428`；低 token 直通 |
| `tests/module/test_summarization_comprehensive.py` | 140 | 遗留深度套件：切点/预算、FIFO 上限、回退、修剪/去重/定向截断、退化 |
| `tests/module/test_e2e_summarization.py` | 7 | 全图封闭式 e2e：真实 `create_agent` 链（主模型为捕获桩、辅助模型为失败桩）驱动静态回退摘要路径；零网络，窗口 32 000（按比例缩小），缺少 MAIN_LLM 配置时跳过 |
| `tests/integration/test_interrupt_marker_approach.py` | 11 | 标记语义：摘要消息对在后续压缩中存活；FACT C 固定装置（窗口 26 000 → usable 10 000，截断线 7 000） |

全量进程隔离套件（`uv run python tests/run_tests_split.py`）通过：**2219 passed / 0 failed**（GROUP A 1469P/2S + GROUP B 750P/5D）。

## ⚠️ 诚实与局限

- **`keep=("messages", 10)` 被接受但从未使用。** 构造函数仅为 API 兼容而存储它；尾部保留由预算决定（`PRESERVE_RATIO` × 窗口，夹在 [2 000, 15 000]），加上路由的 `TRUNCATABLE_RECENT_SKIP` 边距。改 `keep` 没有任何效果。
- **纯装饰性导入。** `summarization.py` 顶部的 `json`、`hashlib`、`SUMMARY_TRIM_TOKENS` 与 `AUTO_CONTINUE_PROMPT` 被导入但从未读取；`DEGRADATION_MONITOR_COUNT` 与 `FILE_OPS_SECTION_MAX_CHARS` 在 `config/num.py` 有定义但无人消费。
- **TTL 注册表没有接入生产。** `record_first_seen` / `select_expired` / `truncate_expired`（以及 `PRUNE_TTL_SECONDS`、`TTL_REGISTRY_MAX_ENTRIES`）只有测试在用；中间件只使用 `truncate_to_budget`。对 `agent/` 的 grep 找不到 TTL 三件套的任何生产调用点。注册表同样是易失的（内存态、以 `tool_call_id` 为键、重启即失）。
- **保留但失效的代码。** `_preemptive_check`（:579）与 `_preemptive_truncate`（:1138）已无调用点 —— 它们实现的二档抢先机制已被四路决策取代，仅为参考保留。
- **估算器是 `chars // 4`，不是分词器。** 它刻意保持确定性（测试可复现、预算稳定），按英文/代码混合内容校准；CJK 密集内容会被低估（中文平均更接近 1–2 字符/token 而非 4）。
- **上报值何时胜出。** T3 是唯一由上报用量驱动的触发点（`compute_pressure` 取 max）。T1/T2 的路由决策由估算驱动（仅估算 + 系统提示词开销）；遗留的 `_check_trigger` 子句兜底使用 `max(本地估算, 上报值)`。
- **T3 绝不改写返回的响应。** T3 派发的持久效果是工具输出的原地截断（消息对象与图状态共享）和防抖记账；T3 的 compact 路由 `request.override` 只在本地生效，原始响应始终返回。整个 T3 函数体 fail-open。
- **T4/T5 设计上绕过防抖矩阵** —— 这正是"强制"的意义所在。超过每类 `MAX_OVERFLOW_RETRIES (3)`、或强制压缩步骤自身失败时，原始 provider 异常向上传播（绝不吞掉、绝不被压缩错误顶替）。
- **压缩是 fail-open 的。** `_apply_compression` 内的任何异常都会记日志并吞掉；回合带着未压缩的历史继续。
- **静态回退是启发式的。** 基于关键词的决策/完成分类与从原始工具参数提取路径都是尽力而为；段落骨架有保证，内容质量没有。
- **`_SUMMARY_PREFIX`/`_SUMMARY_SUFFIX`/`<summary>` 标签/`lc_source="summarization"` 是承重的精确字符串。** 后续回合的链式（`_extract_previous_summary`）、修剪停止条件与全部测试套件都按字面匹配它们 —— 不要随手改写。
