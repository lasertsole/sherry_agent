# 🧭 上下文治理：持久化、驱逐、切片与溢出裁剪

[**English**](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> 原始历史如何保持持久、模型可见上下文如何保持精简：逐模型边界与工具返回的写一次持久化、工具结果卸载到磁盘并留下可恢复预览、执行期 `read_file` 切片、人类消息驱逐但 state 保留全文、媒体治理（输入体积上限、每请求能力擦洗、静默降级检测、压缩期卸载与逐块 token 分型）、在任何压缩路由之前运行的不调 LLM 溢出尾部裁剪，以及把旧摘要挡在对话载荷之外的链式摘要过滤。

Agent 产出的每条消息都有双重价值：既是**原始历史**（真实发生过什么，用于搜索与压缩），也是**模型上下文**（当下能塞进窗口的部分）。本页记录调和这两种需求的各项机制 —— 它们共享同一条规则：**载荷一旦进入流水线就绝不丢失，压缩的只是模型视图，且每次缩减都留下指回全文的指针。**

**事实来源：** `agent/middlewares/context_eviction/core.py`、`agent/middlewares/message_persistence/core.py`、`agent/middlewares/message_persistence/prepare.py`、`pub/func/message/eviction.py`、`pub/func/message/overflow_clip.py`、`pub/func/message/target_truncation.py`、`pub/func/message/tool_args_truncate.py`、`agent/middlewares/summarization/core.py`、`agent/middlewares/summarization/media_offload.py`、`agent/middlewares/media_pipeline/scrub.py`、`agent/middlewares/media_pipeline/degradation.py`、`agent/middlewares/media_pipeline/media_handlers.py`、`agent/middlewares/llm_capability_cache.py`、`agent/middlewares/llm_retry/core.py`、`pub/func/estimate_tokens.py`、`context_engine/store/core.py`、`config/features/agent_side/tool_result_eviction.py`、`config/features/agent_side/summarization.py`、`config/features/agent_side/media_pipeline.py`、`config/features/agent_side/token_estimation.py`。以下每条结论均已与上述代码逐条核对。

## 目录

- [总览与流水线](#-总览与流水线)
- [信息来源](#%EF%B8%8F-信息来源)
- [驱逐、媒体与溢出](eviction/README.zh.md)
  - [💾 逐边界持久化](eviction/README.zh.md#-逐边界持久化)
  - [🗜️ 工具结果驱逐（P0-2）](eviction/README.zh.md#%EF%B8%8F-工具结果驱逐p0-2)
  - [✂️ `read_file` 切片（P2-4）](eviction/README.zh.md#%EF%B8%8F-read_file-切片p2-4)
  - [📥 人类消息驱逐（P1-9）](eviction/README.zh.md#-人类消息驱逐p1-9)
  - [🖼️ 媒体治理（卸载、引用与 token 分型）](eviction/README.zh.md#%EF%B8%8F-媒体治理卸载引用与-token-分型)
  - [⚡ 溢出尾部裁剪（P1-2）](eviction/README.zh.md#-溢出尾部裁剪p1-2)
  - [🧵 链式摘要过滤](eviction/README.zh.md#-链式摘要过滤)
- [Message 行上的 Turn 身份](#-message-行上的-turn-身份)
- [交互与顺序保证](#-交互与顺序保证)
- [配置](#%EF%B8%8F-配置)
- [测试地图](#-测试地图)
- [清除语义与局限](#-清除语义与局限)

## 🎯 总览与流水线

这些机制构成一条流水线；每一级都进一步压缩模型视图，且都不销毁原始记录：

```text
tool returns
  │  ContextEvictionMiddleware.wrap_tool_call
  │    generic tool, text > 20 000 chars → full text to evicted/, head+tail preview in state (P0-2)
  │    read_file                         → execution-time 4 000-char head slice, no file written (P2-4)
  ▼
graph state (tool results: preview only · human messages: full text + lc_evicted_to tag)
  │
  │  MessagePersistenceMiddleware
  │    wrap_tool_call  → flush the RAW result the moment the handler returns
  │    after_model     → flush new human/ai/tool messages at every model boundary
  ▼
MesMemory (full text; persisted_message_ids watermark = write-once)
  │
  │  context pressure triggers Summarization (T1–T5)
  │    1. tail clip      — no LLM: stub the trailing contiguous ToolMessage batch (P1-2)
  │    2. existing route — truncate_tool_results_only / compact_only / compact_then_truncate
  │    3. forced recovery (T4/T5 provider errors) — clip first, then compact + budget truncate, then retry
  ▼
session end → clear_session() removes the session folder (evicted/ + plans) and the watermark
```

| 阶段 | 机制 | LLM 成本 | 对模型视图的影响 |
|---|---|---|---|
| **工具返回** | `ContextEvictionMiddleware`（P0-2 / P2-4） | 无 | 通用结果 > 20 000 字符 → head/tail 预览 + 文件指针；`read_file` → 4 000 字符切片 + 提示 |
| **模型边界** | `MessagePersistenceMiddleware` 的 `after_model` | 无 | 全文写入 MesMemory（不动 state） |
| **人类消息** | `ContextEvictionMiddleware`（P1-9） | 无 | 仅请求视图截断为预览；state/MesMemory 保留全文 |
| **溢出（首步）** | `clip_overflow_tail`（P1-2） | 无 | 尾部工具结果替换为 stub；消息身份与配对不变 |
| **溢出（既有路由）** | `summarization` 四路由分发 | compact 类路由调一次辅助 LLM | 截断与/或历史压缩 |
| **溢出（提供商错误）** | T4/T5 强制恢复 | 每个 compact 步骤一次调用 | 先裁剪 → 再压缩 + 预算截断，最多重试 3 次 |
| **媒体卸载（压缩）** | `offload_inline_media` | 无 | 被摘要前缀中的内联媒体 → 落盘副本 + `[evicted to: …]` 指针；保留窗口不动 |


**浏览器页面遵循同一条规则。** agent 可控浏览器
（`config/features/infra_side/browser_agent.py`）向本管线贡献的东西只有一件：`browser_*`
工具返回的文本（以及截图路径）。页面本身——它的 DOM、像素、screencast 帧——从不进入消息
存储：帧走 `/browser/ws` 直接给面板，快照在成为工具结果之前就被管理器按上限截断（元素 /
文本），截图会以图像本身送到有视觉能力的模型（`image_to_text` 技能只是无原生视觉模型的兜底，且每轮的历史清理会让旧截图不再进入后续上下文）。页面显示的任何内容都只能
经由"模型自己要求的工具结果"影响 agent——而这些结果和其他远程内容一样，会经过不可信输出
围栏与密钥脱敏（见威胁模型）。

## 🗂️ 信息来源

进入图 state 或 MesMemory 的一切信息都来自下列来源之一。`origin` 列正在升级为全量来源标记：`NULL` 是标记之前写入的存量用户消息（读侧视同 `user`），携带 `internal=True` 的消息**不是用户请求** —— 摘要的 Unresolved 清单只收用户亲发消息（正向识别）。非消息来源（驱逐文件、计划知识）也一并列出：它们永远不会成为 `messages` 行，但同样是可注入的上下文。标注 `planned` / `reserved` 的行尚未实现。

| 信息来源 | origin / 标记 | internal | 产生场景 | 持久化 | 注入行为 |
|---|---|---|---|---|---|
| 前端 WS 用户消息 | `origin='user'` | — | 用户在前端发消息 | `messages` 行 `origin='user'` + 全文（逐边界落库） | state/MesMemory 常驻；模型视图可驱逐为预览；摘要逐字保留最新用户请求（`latest_user_request`；多请求清单 + 逐请求驱逐指针：`planned`） |
| 渠道用户消息（QQ 等） | `origin='user'` | — | 用户经渠道适配器发消息 | 同上 | 同上 |
| TaskIntent 引导 / 提醒 | `origin='task_intent'` | `True` | 计划活跃引导 / 任务意图武装（`task_intent/core.py::_task_intent_message`） | `messages` 行 | **非用户请求** —— 不入 Unresolved 清单 |
| 子代理完成载体 | `origin='subagent_completion'` | `True` | 后台子代理完成并回传结果 | `messages` 行（origin 由持久化缝线落标，`context_engine/store/core.py`） | 非用户请求；模型视图可见 |
| 心跳触发的轮次 | `origin='heartbeat'` | — | 心跳服务的会话轮次（**当前不存在此路径 —— `reserved`**） | — | 非用户请求 |
| cron 触发的轮次 | `origin='cron'` | `True` | 定时任务的会话轮次（`origin_for_source`） | `messages` 行 | 非用户请求 |
| 压缩摘要对 | `lc_source='summarization'`（在 `additional_kwargs`，非 origin 列） | — | 压缩产物（`_build_new_messages`） | **不落 MesMemory**；state 摘要对 | `<summary>` 常驻模型视图；以 `<prior-summary>` 链式延续 |
| 驱逐文件 | 非消息 —— 磁盘文件 | — | P0-2 / P1-9 驱逐 | `SESSIONS_DIR/<session_id>/evicted/`（字节级全文） | 按需 `read_file`；摘要链在结构化摘要文档中携带 `evicted_refs[]` 指针 |
| 媒体文件 | 非消息 —— 磁盘文件 | — | 上传处理（`MultimodalProcessor`）、历史消息原生块剥离，或压缩期卸载（`offload_inline_media`） | `SESSIONS_DIR/<session_id>/media/`（持久副本；压缩期卸载以 `sha256[:16]` 命名并去重）；超过 `max_media_bytes` 的载荷绝不写入 | 原生模式下作为媒体块；技能路径提示与每请求擦洗占位携带路径；摘要链把卸载路径保留在 `evicted_refs[]` |
| 计划知识 | 非消息 —— 磁盘目录 | — | plan extraction | `workspace/knowledge/plans/<plan_key>/` | 按 `plan_ref` 注入 `<knowledge>` 块 |
| FACTS.md | `workspace/memory/FACTS.md`（唯一全局文件；memory 工具 target `facts`） | — | 系统级 / 跨项目普适的坑与约定：压缩时记忆回顾 + 计划完成抽取 | memory 文件（1 375 字符上限；超限先淘汰最旧条目） | 作为常驻 FACTS 记忆块注入每次系统提示词 |

## 🧵 Message 行上的 Turn 身份

每条 message 行携带两种不同的 turn 标识：

| 标识 | 粒度 | 写在哪里 |
| :--- | :--- | :--- |
| `turn_num` | 每次 persist batch | `messages.turn_num` |
| `turn_id` | 一次 conversation turn | `messages.turn_id` 与 `additional_kwargs["turn_id"]` |

`_persist_batch` 通过 server 注册的 `GET_CURRENT_TURN_ID` 运行时缝隙（`turn_scope`）取值，把同一个 `turn_id` 写到该轮的所有 batch；因此多个 `turn_num` 可以共享一个 `turn_id`。hook 由 `server.service.trajectory_store.install_turn_id_hook` 注册；没有 server 组装或没有活跃 scope 时降级为空字符串，旧行保持 `NULL`。`(session_id, turn_id)` 部分索引跳过这两种值。它是 `messages` 与 `trajectory_events.turn_id` 的 join 键；测试见 `tests/context_engine/test_turn_id_stamp.py`。

## 🔗 交互与顺序保证

顺序保证（已在 `agent/core.py` 核实，列表顺序 = 注册顺序）：

| 钩子阶段 | 此处关键的顺序 |
|---|---|
| `before_agent`（列表顺序） | `MultimodalProcessor` 在模型循环**之前**运行，因此 P1-9 打标总能见到媒体提示已合并的最终文本 |
| `before_model`（列表顺序） | P1-9 打标先于 `ToolCallNormalize` / `SubagentCompletionDrainMiddleware` |
| `wrap_model_call`（外→内） | ContextEviction（P1-9 视图替换）→ … → Summarization → InvalidToolCallRepairMiddleware（最内层，最靠近 LLM） |
| `after_model`（逆序） | `MessagePersistenceMiddleware` 是模型之后的**第一个**钩子 —— AI 消息先落库，然后 HITL 才会剥离被拒工具调用或抛出 `GraphInterrupt` |
| `wrap_tool_call`（外→内） | IterationBudget → ToolGuardrails → ContextEviction → PathGuard → HeartbeatStaleness → HumanInTheLoop → **MessagePersistence（最内层）** —— 原始结果先刷盘，往外传递时才换成预览 |

交互地图：

| 交互机制 | 发生什么 |
|---|---|
| **Summarization / 压缩** | P1-9 让全文留在 state，压缩依然看得到它；摘要对被挡在持久化与下一次 `<conversation>` 之外 |
| **P1-2 溢出尾部裁剪** | 只经 `model_copy` 替换 `ToolMessage` 内容；绝不触碰 `HumanMessage`，因此 `lc_evicted_to` 标记与 state 全文在每次裁剪中存活；stub 把 P0-2 / P2-4 标记向前携带 |
| **链式摘要过滤** | 只从序列化对话中移除 `lc_source="summarization"` 消息；state 转录与 MesMemory 存储不受影响 |
| **HITL** | 拒绝消息绕过工具返回刷盘（HITL 从外侧包裹持久化），在下一个边界落库；持久化批次会重新挂回被拒工具调用，使拒绝保留配对的 AI 行。HITL 从不接触 `HumanMessage`，因此驱逐与它正交 |
| **`message_search`** | FTS5/SQLite 搜索运行在 MesMemory 上，那里归档着**完整**工具结果与**完整**人类文本 —— 驱逐只压缩模型视图 |
| **前缀缓存** | P1-9 按 id 原地更新消息（内容相同、id 相同）且不重写其他内容，因此只有当预览视图确实不同时才使模型可见前缀失效；工具驱逐发生在消息进入 state 之前，模型见到的自始至终只有预览这一版 |
| **工具配对 / 净化器** | 所有替换都保留 `id` 与 `tool_call_id`；`sanitize_tool_use_result_pairing` 永远不需要为驱逐做修复，stub 依旧是合法的配对输入 |
| **子代理会话** | 子代理管线**不**注册 `ContextEvictionMiddleware` 与 `MessagePersistenceMiddleware`：子转录保留完整工具结果，且只存在于 checkpoint |

## 🧱 稳定系统提示词与提醒层

系统提示词只承载**稳定**内容：persona 文件、memory 块与技能索引。所有**实时**视图——计划模式、工作目录、项目指令（`AGENTS.md`，回退 `CLAUDE.md`）、todo 列表、活动工作指针、TaskFlow 看板、连续性上下文、知识索引——改为在每轮首个模型调用时以 `<system-reminder source="…">` HumanMessage 注入（`DynamicReminderMiddleware` + `agent/middlewares/reminder_catalog/` 目录）。两个原因：首个 token 每轮都变的提示词会让提供方的前缀缓存从那里起全部失效（persona 也被连累）；而只在缓存未命中时重建的提示词，在下一次重建之前展示的是**过期**的 todo 列表。

冻结的记忆快照有一个刻意的例外。切换会话模型会进入新的提供方缓存命名空间；切换思考模式经实测在同一前缀上把 `cache_read` 打到 0——两种情况下缓存本来就要冷。`ThinkingControlMiddleware` 的 `before_model` 钩子比较本轮 `(thinking key, profile fingerprint, mapping fingerprint)` 与上次实际应用值（进程级保存，因为图每轮重建），变化时在 `system_prompt_injection` 之前重载 `MemoryStore` 快照并从两个寄存器删除 `StateKey.SYSTEM_PROMPT`。切换后的首轮首个模型调用因此能看到会话中途写入的 `FACTS.md` / `MEMORY.md` / `USER.md`。首轮、控制未变、重载失败都保留原缓存；没有发送就不会触发。

FACTS.md 是全局文件，目录切换不再使缓存提示词失效。同一轮的 `project_instructions` 提醒仍会重新读取新项目的 `AGENTS.md`（回退 `CLAUDE.md`），并靠 `only_if_changed` 每份不同内容只注入一次。

| 关注点 | 规则 |
|---|---|
| 注入什么 | 每个 builder 返回文本的来源各注入一条提醒：预算顺序为高→低，投递时反转为低→高，使最高优先级位于尾部；标记 `origin=<来源 id>`、`provenance="reminder_catalog"`、`internal=True` |
| 预算 | 每次调用全部来源合计 `REMINDER_CATALOG["total_budget_chars"]`（30 000）——超预算时整条丢弃优先级最低者，绝不把一条提醒截成半句 |
| 节流 | 来源可声明 `cooldown_turns`（TaskFlow 看板 / boulder：5）与 `only_if_changed`（内容相同不重复注入）；节流状态只为**真正注入过**的来源推进 |
| 压缩之后 | 压缩会重置目录，于是下一轮从 live state 重新注入每个来源——提醒层不依赖历史是否幸存 |
| 转义 | 载荷内嵌套的 `<system-reminder>` 标签（todo 文本可被用户影响）会被转义，否则载荷可提前闭合标签、冒充这一层 |
| 轮内定位 | 只在每轮首个模型调用注入——与 TaskIntent 相同的「最后一条消息是真实用户消息」判定；遍历人类消息的扫描器（`split_into_turns`、HITL 无操作者判定、摘要标签）通过 `pub.func.message.system_reminder` 跳过提醒 |

todo 列表还多一道保险：`PlanContinuationEnforcer`（由 `TodoContinuationEnforcer` 更名；它监督的"计划"= todos + TaskFlow flows）统计未调用 `todowrite` 的轮数，超过 `TODOLIST_INFRA["todo_write_nudge_turns"]`（10）就在续跑指令后追加 `[REMINDER: TODO TRACKING]` 提醒——这是基于快照的停滞检测唯一看不见的形态（模型推进 TaskFlow 会让 streak 清零，而 todo 列表在腐烂）。

## 🧹 checkpoint 保留与孤儿 session 清理

主代理与子代理共用 `src/checkpoints/sqlite.db`。主代理 turn 结束后，`on_turn_finished` 把该 session 的 thread 裁到最新顶层 checkpoint 及其 writes（即 `aget_state` 读取的那一份）；child 的 `_release_child_resources` 对自己的 thread 做同样处理。`tools:*` / `model:*` 行会删除；没有顶层行时保留最新行兜底。HITL pending 的 session 会跳过，因为挂起的图不算 turn 结束。删除主 session 时先抑制并 kill 所有后代 run，再删除每个后代的 registry 行、checkpoint thread、session 目录、计划数据和附件。启动时 `init_registry()` 之后执行一次 sweep，删除根主 session 或父 run 已不存在的所有 run 子树。所有清理路径都 fail-open 且幂等。

## 📏 压缩线与上下文用量

压缩线是由 `agent/middlewares/summarization/thresholds.py` 统一计算的**绝对 token 数**：设置 `MAIN_LLM_MAX_OUTPUT_TOKEN` 时为 `window - max_output`；未设置时保留原有 80% 行为（未设置时 overflow router 仍使用预算相对区间）。`GET /context_usage` 返回 `max_output`、`compress_tokens` 和 `compress_ratio = compress_tokens / window`，圆环与红色虚线使用该比例。最大输出窗口大于等于上下文窗口时 `/models-config` 拒绝保存；旧文件在运行时钳到 0% 线。`compression_reserve_tokens` 仍是压缩后的目标预算，不再参与压缩线。

## 🛠️ 配置

`TOOL_RESULT_EVICTION`（`config/features/agent_side/tool_result_eviction.py`）：

| 键 | 默认值 | 含义 |
|---|---|---|
| `enabled` | `True` | 工具结果驱逐（P0-2）总开关 |
| `evict_threshold_chars` | `20_000` | 文本超过该字符数即驱逐 |
| `preview_head_lines` / `preview_tail_lines` | `5` / `5` | 预览 head/tail 行数 |
| `eviction_subdir` | `"evicted"` | `SESSIONS_DIR/<session_id>/` 下的子目录 |
| `excluded_tools` | 8 个名字 | 永不驱逐（`read_file` 改走切片路径） |
| `human_evict_enabled` | `True` | 人类消息驱逐（P1-9）总开关 |
| `human_evict_threshold_chars` | `200_000` | 人类消息触发阈值 |
| `human_preview_head_lines` / `human_preview_tail_lines` | `5` / `5` | 人类消息预览 head/tail 行数 |

`SUMMARIZATION`（`config/features/agent_side/summarization.py`）中本页依赖的键：

| 键 | 默认值 | 含义 |
|---|---|---|
| `overflow_clip_enabled` | `True` | P1-2 尾部裁剪总开关 |
| `overflow_clip_max_remove` | `10` | 一次裁剪最多 stub 的尾部消息数 |
| `overflow_clip_min_keep` | `5` | 转录下限：长度不超过它时绝不裁剪 |
| `max_tool_output_tokens` | `500` | 工具结果的压缩期裁切预算（token；≈2 000 个 ASCII 字符） |
| `content_head_ratio` / `content_tail_ratio` | `0.3` / `0.3` | 压缩期裁切的 head/tail 保留比例 |

`TOKEN_ESTIMATION`（`config/features/agent_side/token_estimation.py`），多模态 token 分型：

| 键 | 默认值 | 含义 |
|---|---|---|
| `chars_per_token` / `chars_per_token_cjk` | `4` / `2` | 文本估算除数（非 CJK / CJK） |
| `tokens_per_image_block` | `85` | 每个图像块的固定成本（与 `langchain_core.count_tokens_approximately` 对齐） |
| `tokens_per_audio_block` / `tokens_per_video_block` | `256` / `1024` | 保守固定成本（估算时无时长元数据） |
| `tokens_per_unknown_block` | `85` | 未识别块的固定成本 —— 绝不是它的 base64 |

`MEDIA_PIPELINE`（`config/features/agent_side/media_pipeline.py`）中入口侧相关的键：

| 键 | 默认值 | 含义 |
|---|---|---|
| `main_llm_native_multimodal` | `"auto"` | 三态原生开关：`"true"` 为模型保留媒体块、`"false"` 始终走技能路径、`"auto"` 经能力缓存逐媒体族决定；其他值一律 fail-safe 走技能路径 |
| `main_llm_silent_degradation_detection` | `True` | 原生回复自述媒体盲时，把请求中实际出现的媒体族缓存为 `"unsupported"` |
| `max_media_bytes` | `20 * 1024 * 1024` | 单个载荷硬上限；超限载荷在任何写盘之前即被跳过 |

这些旋钮没有环境变量：按设计它们就是上述 feature TypedDict 中的代码默认值。

## 🧪 测试地图

| 测试套件 | 覆盖内容 |
|---|---|
| `tests/agent/middlewares/context_eviction/test_context_eviction.py` | P0-2/P2-4 中间件行为：驱逐、排除、切片、水位覆盖、失败开放 |
| `tests/agent/middlewares/context_eviction/test_human_eviction.py` | P1-9 打标、reducer 原地更新、模型视图截断、自愈、媒体保留 |
| `tests/agent/middlewares/message_persistence/test_message_persistence.py` | 边界持久化、水位写一次、拒绝消息重新配对 |
| `tests/agent/middlewares/message_persistence/test_tool_result_persistence.py` | 工具返回刷盘及其与无 id 指纹 / 标记的交互 |
| `tests/agent/middlewares/message_persistence/test_compression_no_persistence.py` | 压缩路径不向 MesMemory 写任何东西 |
| `tests/pub/func/message/test_eviction.py` | 纯驱逐原语：阈值、预览、幂等、不安全会话 id |
| `tests/pub/func/message/test_read_file_slice.py` | P2-4 执行期切片及其幂等性 |
| `tests/pub/func/message/test_overflow_clip.py` | P1-2 纯裁剪：尾部批次检测、闸门、token 目标、标记保留 |
| `tests/agent/middlewares/test_summarization_overflow_clip.py` | P1-2 中间件集成：零 LLM 恢复、降级、T4/T5、同步/异步奇偶 |
| `tests/agent/middlewares/test_summary_message_filtering.py` | 链式摘要过滤与 `<prior-summary>` 注入 |
| `tests/agent/middlewares/test_compression_media_offload.py` | 压缩期内联媒体卸载：写盘 + 指针、hash 去重、失败占位、保留窗口不动、同步/异步、`evicted_refs` |
| `tests/pub/func/test_estimate_tokens_media.py` | 逐块多模态估算：固定媒体成本、5 MB base64 回归、未知块藏媒体、`str` / `None` / 空列表边界 |
| `tests/agent/middlewares/test_multimodal_processor.py` | 三态原生开关与每请求擦洗：混合族只剥离 unsupported 块、supported 块原样保留、state/kwargs/磁盘不动、`"true"` 永不擦洗、历史图片剥离 |
| `tests/agent/middlewares/test_media_size_limit.py` | `max_media_bytes` 上限：超限载荷在写入前跳过、模型可见提示、恰好等于上限允许、声明的 `Content-Length` 快路径、限量读取 |
| `tests/agent/middlewares/test_media_degradation.py` | 静默降级检测：en / zh / ja / ko 盲区正则 + 请求描述模式、有能力或无关回复不误报、实际出现的媒体族全部写缓存、标志无论是否命中都清除、回退候选归属 |
| `tests/agent/middlewares/test_multimodal_native_fallback_e2e.py` | 拒绝 → 缓存 + 技能路径改写：后续会话跳过原生、模型 key 隔离、显式 `"true"` 永不回退 |
| `tests/context_engine/store/test_persisted_message_ids.py` | `persisted_message_ids` 水位存储 |
| `tests/full/test_context_governance_e2e.py` | 实网 e2e（真实 LLM + 真实图）：各项机制端到端 —— 显式运行 |

```bash
# Hermetic suites (CI gate)
uv run pytest tests/agent/middlewares/context_eviction \
    tests/agent/middlewares/message_persistence \
    tests/pub/func/message/test_eviction.py \
    tests/pub/func/message/test_read_file_slice.py \
    tests/pub/func/message/test_overflow_clip.py \
    tests/agent/middlewares/test_summarization_overflow_clip.py \
    tests/agent/middlewares/test_summary_message_filtering.py \
    tests/agent/middlewares/test_multimodal_processor.py \
    tests/agent/middlewares/test_media_size_limit.py \
    tests/agent/middlewares/test_media_degradation.py \
    tests/agent/middlewares/test_multimodal_native_fallback_e2e.py \
    tests/agent/middlewares/test_compression_media_offload.py \
    tests/pub/func/test_estimate_tokens_media.py \
    tests/context_engine/store/test_persisted_message_ids.py -q

# Live-network e2e — RUN EXPLICITLY, never part of the CI gate
uv run --no-sync pytest tests/full/test_context_governance_e2e.py -v
```

## 🧹 清除语义与局限

- **`clear_session` 一次清除全部。** `server/service/session_cleanup_service.py::purge_session` 删除该会话的 MesMemory 行（连同 `persisted_message_ids` 水位一起删除）、checkpointer 历史，以及整个 `SESSIONS_DIR/<session_id>/` 文件夹 —— 因此 **`evicted/` 文件与 `plans/` 随会话一并删除**（`config/path.py::session_plans_dir` 记录了同样的整目录契约）。内存寄存器最后清理。
- **驱逐只是模型视图压缩，绝不是删除。** 本页压缩的每份载荷要么归档在 MesMemory、要么全文留在图 state（人类消息）、要么在磁盘 `evicted/` 下 —— 且每份预览都带指针。
- **`evicted/` 目录归会话所有，但没有垃圾回收。** 文件活到 `clear_session` 为止；没有逐消息 TTL。长会话 + 大量巨型工具结果会在 `workspace/sessions/<session_id>/evicted/` 下持续累积磁盘占用。
- **行窗口缩不下去的载荷回退到按字符预算的预览。** 行数 ≤ 10 时 5 头/5 尾窗口重叠、会把原文抄两遍（无换行的正文是极端情形）；回退方案改为把每行截到 2 048 字符、每侧截到 8 192，于是 50 000 字符的单行仍会以约 4 300 字符的预览被驱逐。`len(preview) >= len(text)` 守卫保留为最终兜底。
- **`read_file` 切片不可从切片自身恢复 —— 这是设计。** 源文件才是恢复路径；提示明确告诉模型如何续读。只有文件缺失/被删才会失效。
- **人类消息驱逐仅限尾部消息。** 巨型载荷之后如果还有另一个用户回合，它不会被重新检视；"只看最后一条"是刻意设计，避免重翻已定历史。
- **尾部裁剪只有在尾部批次够大时才有用。** 若上下文被人类回合或非工具消息占满，则退化为既有路由；P1-2 是优化，不是保证。
- **子代理转录不在范围内。** 子代理保留完整工具结果（不驱逐、不持久化）—— 其转录只存在于 checkpoint，永不进入客户端可见的 MesMemory 历史。
