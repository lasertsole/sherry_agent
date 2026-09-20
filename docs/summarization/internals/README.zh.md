# 🧠 摘要压缩内部机制 — 截断、压缩、摘要与护栏

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> [Summarization](../README.zh.md) 的一部分：内部轨道 —— 预算截断与 TTL、压缩管线、媒体卸载、LLM 摘要链、静态回退、输出消息对、防抖护栏矩阵、系统提示词刷新与注册点。

---

## ✂️ 截断轨道：预算截断与 TTL 模块

`_run_budget_truncation`（:659）内部按顺序运行两层截断：

**第 1 步 —— 工具调用参数**（`pub/func/message/tool_args_truncate.py`）：每条 JSON 序列化后超过 `MIN_ARGS_CHARS_TO_TRUNCATE (500)` 字符的 `AIMessage.tool_calls[].args` —— 且其工具不在 `PROTECTED_TOOLS` 中 —— 会被替换为 `{"_truncated_args": "head…[args truncated, omitted N chars]…tail"}`，并以 `MAX_TOOL_ARGS_CHARS (2_000)` 字符封顶（头部 30% / 尾部 30%，与工具输出比例相同）。这样 `args` 仍是 dict（LangChain 的 `ToolCall.args` 类型），对每个 provider 适配器都保持 JSON 可序列化，模型也能看出参数被切过。最近 `TRUNCATABLE_RECENT_SKIP (6)` 条消息会被跳过，被替换的 `AIMessage` 是 `model_copy` 克隆 —— tool_call_ids 绝不触碰，AIMessage↔ToolMessage 配对保持完好。

**第 2 步 —— 工具输出**：`pub/func/message/tool_result_ttl.py` 提供截断轨道使用的原地截断。设计不变量（承重）：

- **只原地修改** —— 该模块从不删除、重排或弹出消息；只修改 `msg.content`（或 content 列表块）并返回索引。这保住了 provider API 与 `ToolCallNormalize` 依赖的 tool-call/`ToolMessage` 配对。
- **占位符非空** —— 被截断的结果始终保留非空内容：`ToolCallNormalize.before_model` 会**丢弃空的 `ToolMessage`** 来净化转录，空占位符会悄悄破坏配对。
- **头部 30% / 尾部 30% 保留**（`CONTENT_HEAD_RATIO` / `CONTENT_TAIL_RATIO`）加省略标记。

中间件实际消费的部分：`truncate_tool_args`（第 1 步，参数）与 **`truncate_to_budget`**（第 2 步，工具输出），由路由的候选列表驱动 —— `_run_budget_truncation`（:659）按预算（`usable × TRUNCATE_BUDGET_RATIO`）截断候选，直到达标。因为第 1 步返回的是新的 `AIMessage` 而非原地修改，该函数返回最终列表，每个调用方**必须**把该列表喂给 `request.override`。

**read_file 结果保持可找回**：`pub/func/message/target_truncation.py` 中的头+尾截断（由非 LLM 策略 `_run_non_llm_strategies` 执行）会按 `tool_call_id` 把每条 `ToolMessage` 反查回 AIMessage 的工具调用；当工具是 `read_file` 且 `args.file_path` 存在时，被切掉的中段会替换为找回通知而非匿名标记。该通知沿用同样的头部 30% / 尾部 30% 比例，写出原始 `file_path`，并给出 `Use offset=<N> to continue reading: read_file(file_path='<path>', offset=<N>, limit=500)`。`N` 是**头段未完整保留的第一行的绝对（1-based）文件行号** —— 因此以 `offset=100` 读取的页会从头部真正停止的位置继续，被切在半途的行会被重读、绝不会跳过。无法推算 offset 时（载荷不是 read_file 的 JSON 结果），通知要求从 `offset=1` 重新分段读取，绝不猜测 offset。其余工具逐字节保留匿名 `...[truncated N chars]...` 标记。

TTL 注册表本体（`record_first_seen` / `select_expired` / `truncate_expired`、`PRUNE_TTL_SECONDS = 300`、`TTL_REGISTRY_MAX_ENTRIES = 512`、以 `tool_call_id` 为键、重启即失）如今**只有测试套件在用** —— 中间件没有接入任何按龄过期的逻辑（见"[诚实与局限](../README.zh.md#%EF%B8%8F-诚实与局限)"）。

## 🔁 压缩轨道：`_apply_compression` 内部

`_apply_compression`（:1688；异步孪生 :1760）按顺序执行：

1. **捕获恢复上下文**（`_capture_recovery_context`，:1577）：最后一条用户请求（≤ 800 字符）与文件操作棘轮 —— 从 `read`/`write` 族工具调用中提取路径（:415），与上一轮的集合合并（读过的会记住，改过的文件绝不会被降级为只读）。
2. **非 LLM 策略**（`_run_non_llm_strategies`，:1493）：`去重 → 修剪 → 定向截断 → 工具参数截断`（细节见 [截断轨道](#-截断轨道预算截断与-ttl-模块)）。这些是免费的 —— 不调模型。
3. **是否用 LLM 的决策**：

   ```
   if tokens_after_non_llm > budget × 2  OR  skip_llm  OR  nothing was reduced:
       summarize [0:cutoff] and rebuild   → strategy "llm_summary" / "fallback"
   else:
       keep as-is                          → strategy "non_llm_sufficient"
   ```

   非 LLM 收缩先拿走第一次机会；只有当历史仍超过保留预算的两倍（或防抖控制器已禁用 LLM 摘要，或非 LLM 策略毫无所得）时才花费辅助 LLM。
4. **激进兜底**（`_aggressive_truncate`，:1539）：如果结果*依然*过大，每个超过 `AGGRESSIVE_TRUNCATE_CHARS (1 000)` 字符的 `ToolMessage` 被硬切并加标记 —— 每一个超过同一上限的工具调用参数 JSON 也一样（只保留头部，`PROTECTED_TOOLS` 豁免，替换为 `{"_truncated_args": ...}`）。
5. **摘要自截断**（`_truncate_summary_messages`，:1630）：任何超过 `SUMMARY_TOTAL_MAX_CHARS (16 000)` 字符的既有摘要消息（`lc_source == "summarization"`）被重新截断为头部 30% / 尾部 30%（`_truncate_content`，:1622）。
6. **恢复注入**（`_inject_recovery_context`，:1595）：捕获的文件操作棘轮被改写进摘要的 `## Relevant Files` 段，检查点始终携带最新的读/改文件地图。
7. **记账**（`_record_compression`，:1277，最后 `request.override(messages=..., system_message=...)`。

### 💾 压缩时 nudge

消息持久化在压缩路径之外运行：human/AI 消息在每个模型调用边界、工具结果在返回时，都由 `MessagePersistenceMiddleware`（`agent/middlewares/message_persistence/`）增量落库到 MesMemory，靠持久水位 `persisted_message_ids` 保证写一次。一次 compact 只做压缩并调度下面的 nudge。触发语义详见 `agent/middlewares/README.md`。

**压缩时 nudge**（`agent/middlewares/summarization/nudges.py::schedule_compression_nudges`）：记忆回顾（`_nudge_memory`）每次压缩都派发；计划提取在同一时点评估 `_detect_todo_all_complete`。两者都以 fire-and-forget 方式在 NUDGE 车道上派发，绝不可能阻塞模型调用。nudge 锁被持有时压缩完全跳过派发（不排队）。单发 `nudge_plan_extraction_fired` 标记保证每个完成周期只提取一次 —— 因此从不压缩的会话永远不会触发计划提取。

**切点选择**（`_determine_cutoff`，:1310）：把历史切成回合，**从最新往回**累加、对照保留预算 `clamp(window × 0.25, 2 000, 15 000)`（`_calculate_preserve_budget`，:565）；放不下的整回合可以从中劈开。`_adjust_for_orphan_pairs`（:1340）再把切点往回走，直到没有 `ToolMessage` 与它的 `AIMessage` 工具调用分离。除非最后一回合比例闸门触发（最后一条用户消息 ≥ token 总量的 `LAST_TURN_RATIO_THRESHOLD (0.5)` —— `_check_last_turn_ratio`，在 wrap 入口 :1968/:2054 调用），切点绝不越过最后一条 `HumanMessage`。

所有失败模式都是 fail-open：`_apply_compression` 抛异常只会记日志，原始请求原样继续 —— 坏掉的压缩从不弄坏回合。

## 🖼️ 压缩期媒体卸载（内联媒体 → 引用）

压缩绝不能把 base64 载荷交给辅助摘要模型。在序列化被摘要的前缀（`current_messages[:cutoff]`）之前，`offload_inline_media`（`agent/middlewares/summarization/media_offload.py`）只重写**该范围**内的每个内联媒体块：

- `data:` URL / base64 载荷（`image_url` / `audio_url` / `video_url`、裸 `base64` 字段、`audio_bytes` / `video_bytes`，或 Anthropic 风格的 `source.data`）被解码并写入 `SESSIONS_DIR/<session_id>/media/{sha256(raw)[:16]}{ext}`；扩展名由魔数经 `media_handlers._infer_extension` 推导；
- 相同字节只写一份 —— 以内容 hash 命名的文件名在单次及多次压缩之间去重；
- 该块被替换为文本指针 `[evicted to: <path>]`，与 `pub/func/message/eviction.py` 发出的标记一致，因此 `_collect_evicted_refs` 能收集到它，`_finalize_summary_doc` 会把路径带进 `SummaryDoc.evicted_refs`（渲染为 *Evicted References*）；
- 无法解码或写入的块变成 `<media error="failed_to_offload" />` —— 媒体失败绝不会弄坏压缩。

保留窗口（`current_messages[cutoff:]`）绝不触碰：媒体只在它的回合真正被摘要时才离开。两条压缩路径都会在 nudge 调度与记忆落库之前对前缀切片调用卸载 —— `_apply_compression_under_lock`（同步）与 `_aapply_compression_under_lock`（异步）。摘要提示词（`_SUMMARY_JSON_RULES` / `_SUMMARY_TEMPLATE`）告诉模型这些指针存在、必须原样带进 `evicted_refs`、不得臆测视觉/音频/视频细节，并可按路径取回载荷。

媒体文件位于会话目录树内，因此 `clear_session()` 会把它们随 `evicted/` 与 `plans/` 一并删除。

## 📝 LLM 摘要：提示词、链式与回退

`_create_summary` / `_acreate_summary`（:1410 / :1435）：

1. **序列化**（`_serialize_for_summary`，:258）：每条消息变成一行带标签的文本 —— `[User]:`（≤ 2 000 字符）、`[Assistant]:`（≤ 2 000 字符）、`[Assistant tool call]: name(args: > 500 chars → head 300 + tail 150 + omission marker)`、`[Tool result|Tool error] (id):`（> 2 000 字符 → 保留 1 800 + 省略标记）。
2. **链上之前的检查点**（`_extract_previous_doc` / `_extract_previous_summary`）：找到最新的 `additional_kwargs["lc_source"] == "summarization"` 的 `AIMessage`，优先读取其结构化 `summary_doc` 载荷（渲染回 Markdown 供 `<prior-summary>` 使用）；没有该载荷的消息 —— 存量会话，或回退到 free-form 的一轮 —— 仍按 `<summary>…</summary>` 正文解析。存在上一份 Doc 时，提示词变为 `conversation + <prior-summary-json> + _SUMMARY_PROMPT_UPDATE_STRUCTURED`；旧 Markdown 照常经 `<prior-summary>` 注入；升级后的第一轮压缩即输出新的 `SummaryDoc`（无需迁移）。
3. **结构化输出**（`summary_doc.py::SummaryDoc`）：辅助模型经 `with_structured_output(SummaryDoc, method="json_mode")` 包装。当前配置的 `glm-5.3-flash` 端点忽略函数调用 schema（默认 method 返回自由文本，被 Pydantic 解析器拒绝 —— 已实测），因此 `json_mode` 是主档；解析/校验失败退化为原始调用 + `json_repair`（`_sync_json_repair_doc` / `_async_json_repair_doc`）；再失败则以旧的 free-form Markdown 提示词作为最后的 LLM 档，静态回退仍是最终保险。
4. **调用**辅助模型，带 `config={"metadata": {"lc_source": "summarization"}}`，让下游工具链能识别摘要调用。
5. **护栏：**free-form 响应为空或过短时回退到确定性摘要；任何异常同样回退。失败时 LLM 永远没有最终话语权。

**渲染（Doc → Markdown）。** `render_summary_markdown` 是纯函数、确定性（同 Doc → 同字节，前缀缓存安全）：输出旧的节骨架，仅在数组非空时追加 *Active Plan Notes* / *Evicted References*：

| `SummaryDoc` 字段 | 渲染节 | 代码层 cap |
| :--- | :--- | :--- |
| `latest_user_request` | `## Latest Unresolved User Request` | 逐字、无上限 |
| `goal` | `## Goal` | — |
| `constraints` | `## Constraints & Preferences` | — |
| `completed` | `### Completed` | `completed[-5:]` |
| `in_progress` / `blocked` | `### In Progress` / `### Blocked` | — |
| `key_decisions` | `## Key Decisions` | `key_decisions[-5:]` |
| `next_steps` | `## Next Steps` | — |
| `critical_context` | `## Critical Context` | `critical_context[-3:]` |
| `relevant_files` | `## Relevant Files` | — |
| `active_plan_notes` | `## Active Plan Notes`（仅非空时） | `active_plan_notes[-20:]` |
| `evicted_refs` | `## Evicted References`（仅非空时） | `evicted_refs[-20:]` |

cap 是 `cap_summary_doc` 中的数组切片（链上存储的形态）；渲染器在展示时再次切片并追加 `"(N earlier items omitted for brevity)"`。`evicted_refs` 由代码维护：`_collect_evicted_refs` 扫描本压缩范围内的 `[evicted to: <path>]` 标记与人类消息的 `lc_evicted_to` 标签，`_finalize_summary_doc` 把上一份 Doc 的条目与新条目按序去重合并。`_inject_recovery_context` 把文件操作棘轮同时写回渲染出的 `## Relevant Files` 节与存储 Doc 的 `relevant_files` 字段。

### 🗂️ Active Plan Notes 与计划上下文注入

压缩是计划感知的。调用辅助模型前，`_get_plan_context_sync(session_id)`（`core.py`，紧邻 TaskFlow 注入块）把本会话正在执行的计划渲染成权威提示块：

- 计划活跃判定复用 `agent/tools/todolist/knowledge/ownership.py` 的两个原始关联来源 —— 会话的 `plan_ref` state 键与本会话某条 todo 的 `plan_ref`（`plan_context.py::resolve_active_plan`）；**boulder 来源被有意排除**；
- 注入内容为**计划文件相对路径 + 计划名 + 未完成 todo 概要**，绝不注入计划全文（模型可自行 `read_file`）。todos 全部 `completed` / `cancelled` 的会话**不是**活跃计划；
- 首摘与链式更新两条提示词路径（结构化与旧 free-form）都注入该块。

`SummaryDoc.active_plan_notes` 由管线而非模型掌管，保证计划注意事项在计划活跃期全程保留：

- 链式更新时，上一份 Doc 的条目**逐字、按序继承**；模型只能追加新的一行教训（`现象 -> 规避动作`）；
- 数组上限为 `active_plan_notes[-20:]`；链上渲染追加 `(N earlier items omitted for brevity)`，存储载荷保留截尾；
- 计划完成（todos 全部 `completed` / `cancelled`）后，解析器报告"无活跃计划"：渲染不再含 `## Active Plan Notes` 节，下一份 Doc 的数组清空。完全没有 `plan_ref` 的会话同样永不携带该数组。

**最新用户请求逐字 + 驱逐指针。** *Latest Unresolved User Request* 节永不截断：`latest_user_request` 逐字呈现（旧模板里的 `max 800 chars` 指令也已移除）；序列化的 `<conversation>` 来自 **state** —— 被驱逐的人类消息在 state 中保留全文与 `lc_evicted_to` 标签，只有模型视图是预览。当最新用户请求本身被驱逐时，管线把它的 `[evicted to: <path>]` 指针追加到该字段（代码掌管，`_latest_human_eviction_ref`），摘要链因此永远保留回到磁盘全文的路径；内部注入（`metadata.internal`）不会抢走这个锚点。

**链式摘要过滤**（`_filter_summary_messages`）：存在旧检查点时，其 Human/AI 消息对会从序列化的 `<conversation>` 输入中剔除 —— 提取出的旧摘要仅经 `<prior-summary>`（或 `<prior-summary-json>`）注入，同一段旧摘要文本在提示词中只出现一次。该消息对**两条**都带 `additional_kwargs={"lc_source": "summarization"}`，这同时让两条都不落入 MesMemory（`MessagePersistenceMiddleware._is_persistable` + `HumanMessageRowBuilder`）：摘要是压缩的内部产物，不是对话历史。过滤发生在 `_extract_previous_doc` / `_extract_previous_summary` **之后**（链式仍能看到旧摘要），过滤后的列表用于序列化、提示词构建，以及两条静态回退分支（LLM 失败/响应过短）—— 也包括 `_apply_compression_under_lock` / `_aapply_compression_under_lock` 的 `skip_llm` 路径。与 opencode-dev 的 `hidden` 集合、deepagents 的 `_filter_summary_messages` 对齐。

旧提示词模板（`_SUMMARY_TEMPLATE`）仍固定 free-form 回退的骨架 —— *Latest Unresolved User Request / Goal / Constraints & Preferences / Progress（Completed ≤ 5 · In Progress · Blocked）/ Key Decisions ≤ 5 / Next Steps / Critical Context ≤ 3 / Relevant Files* —— 要求"即使为空也保留每一节"并带保密规则（"NEVER include API keys, tokens, passwords, secrets"）。结构化路径以 `_SUMMARY_JSON_RULES`（JSON 字段清单 + 同一保密规则）取代 Markdown 骨架；字段语义直接定义在 Pydantic `SummaryDoc` 模型上。

**用户请求来源的正向识别。** 每个落库的 `human` 行都带 `origin`（在传输入口打标）：WS/渠道用户输入为 `"user"`，编排引导注入为 `"task_intent"`，完成载体为 `"subagent_completion"`，定时投递轮次为 `"cron"`（`ai`/`tool` 行保持 `NULL`；origin 标记上线前的存量行按 user 消息读取）。*Latest Unresolved User Request* 的来源由该列正向识别：只有用户来源消息（`origin = 'user'`，或存量 `NULL`）才算用户请求 —— 内部注入（`task_intent` / `subagent_completion` / `cron`）绝不会被当作请求引用。routing plan 的复数 `unresolved_user_requests[]` 清单使用同一正向过滤。

## 🧱 静态回退（无 LLM 摘要）

`_build_static_fallback_summary`（:296）零模型调用产出同样的段落骨架：

- 最后一条用户请求 → *Latest Unresolved User Request*；第一条请求 → *Goal*；
- 含决策关键词（`decided`、`choosing`、`because`、`therefore`）的 AI 文本 → *Key Decisions*，否则 *Completed*；
- 每个工具调用 → *Completed*；路径样 token（含 `/` 或 `\`，或以 `.py`/`.md`/`.js`/`.ts`/`.json` 结尾）→ *Relevant Files*（≤ 10，排除 `http` 链接）；
- 报错的 `ToolMessage` → *Blocked* 与 *Critical Context*。

`skip_llm` 生效时原样使用它；它也是短/失败 LLM 摘要的安全网。

## 📦 输出：摘要消息对

`_build_new_messages`（:1464）包裹摘要文本，恰好产出两条消息：

```
[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted …
Respond ONLY to the latest user message that appears AFTER this summary.

<summary>
…summary Markdown…
</summary>

--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---
```

- **HumanMessage** `"What did we do so far?"` —— 一个中性问题，维持角色交替；与 AI 半边携带相同的 `lc_source` 标记。
- **AIMessage**，带 `additional_kwargs={"lc_source": "summarization"}` —— 这个标记被后续回合用于：(a) 找到并链起之前的检查点，(b) 在链式再摘要输入中剔除该消息对、并让修剪停在检查点处，(c) 让测试断言被取代后的摘要可以从模型视图整体吞下。
- 该消息对永不进入 MesMemory：`MessagePersistenceMiddleware._is_persistable` 跳过两条 `lc_source="summarization"` 的消息（human 行构造器有同样的闸门）。
- AIMessage 还携带结构化文档本体：`additional_kwargs["summary_doc"]`（cap 后的 `SummaryDoc`，纯 JSON 可序列化 dict —— 链式载体，回喂为 `<prior-summary-json>`）。free-form 回退轮次不带该载荷，`_extract_previous_summary` 回退解析 `<summary>` 正文。
- 总内容以 `SUMMARY_TOTAL_MAX_CHARS (16 000)` 封顶，头部/尾部 30/30 保留。

## 🛡️ 防抖护栏矩阵与退化恢复

状态存放在会话级 `state_register_mem` 的**十三个** `summarization_*` 键中（:92–107）。`_reset_turn_state`（:1849）在每个回合开始时重置其中**十一个**；`summarization_last_user_question` 与 `summarization_cooldown_rounds` 被刻意**不**按回合重置。

| 护栏 | 键 | 阈值 | 效果 |
| :---- | :-- | :-------- | :----- |
| 回合冷却期 | `summarization_cooldown_rounds` | `COMPACTION_COOLDOWN_ROUNDS = 3` | 每次实际 compact 后武装（:694）；**每次**模型调用递减（:832）；封锁 T1 compact 路由、T2 主动压缩与 T3 —— 永不封锁 T4/T5 强制恢复环 |
| 每回合压缩数 | `summarization_turn_attempts` | `MAX_COMPRESS_ATTEMPTS_PER_TURN = 3` | 由 :694 递增；压制 T2 主动压缩 + T3（强制环豁免） |
| 溢出重试（T4/T5 共用） | `summarization_overflow_retries` | `MAX_OVERFLOW_RETRIES = 3` | 两类错误共用、按回合重置；每次成功的强制步骤后递增；耗尽 → 原始 provider 错误向上传播 |
| 会话压缩总数 | `summarization_compression_count` | `MAX_TOTAL_COMPRESSION_ATTEMPTS = 5` | `_should_skip_compression`（:1255）返回 True —— 主动压缩完全停止 |
| 连续无效次数 | `summarization_compression_ineffective` | `INEFFECTIVE_THRESHOLD = 2` | 置 `skip_llm` —— 只跑非 LLM 策略 |
| 有效性判定 | （`_record_compression`，:1277 | 消息数下降**或** token 缩减 ≥ `MIN_EFFECTIVENESS_PCT (0.05)` | 成功的非 LLM 策略（`dedup`/`prune`/`truncate`/`fallback`/`aggressive`）会再次清掉 `skip_llm` |
| 退化恢复预算 | `summarization_recovery_attempts` | `MAX_RECOVERY_ATTEMPTS = 2` | 限制退化监视器发起的强制恢复次数 |

**退化监视器**（`_monitor_degradation`，:1661）：只在本次调用真的发生过压缩时才被咨询（`_compaction_just_happened` 标志）。模型回复没有文本时计数器递增；连续 `DEGRADATION_NO_TEXT_THRESHOLD (3)` 次空回复 —— 且 `summarization_recovery_attempts < 2` —— 时置 `force_recovery`、清零无效连击与会话压缩计数。任何非空回复都会清零计数器。它捕捉的是"压缩 → 模型懵了 → 空输出 → 再压缩"的病态循环。注意二者的配合：force 标志在 wrap 入口（:1973）被读取，**先于** `_should_skip_compression`，而跳过闸门会消费它（重置各计数器并继续，:1256–1261）—— 恢复压缩恰好跑一次。

## 🔄 系统提示词刷新

仅主 agent（`need_update_system_prompt=True`）：压缩后中间件重建系统提示词并写入 `system_prompt` 状态键，让下一次模型调用看到的是当前的人设文件 / 长期记忆。两条送达路径：压缩后直接 `request.override(system_message=SystemMessage(...))`；以及当 T1 已发生过 compact、而防抖闸门又拦下了第二次压缩时，重建的提示词仍会在闸门路径中送达（:1993–2005），因为不带 `@dynamic_prompt` 系统提示词中间件的链路（子 Agent / nudge 管线）依赖本中间件送达它。闸门路径仅在请求当前 system message **内容不同**时才注入：内容相同则不 override、不新建 `SystemMessage`（不会重复注入）。

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
