# 🧭 上下文治理：持久化、驱逐、切片与溢出裁剪

[**English**](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> 原始历史如何保持持久、模型可见上下文如何保持精简：逐模型边界与工具返回的写一次持久化、工具结果卸载到磁盘并留下可恢复预览、执行期 `read_file` 切片、人类消息驱逐但 state 保留全文、在任何压缩路由之前运行的不调 LLM 溢出尾部裁剪，以及把旧摘要挡在对话载荷之外的链式摘要过滤。

Agent 产出的每条消息都有双重价值：既是**原始历史**（真实发生过什么，用于搜索与压缩），也是**模型上下文**（当下能塞进窗口的部分）。本页记录调和这两种需求的六项机制 —— 它们共享同一条规则：**绝不丢数据，只压缩模型视图，并始终留下指回全文的指针。**

**事实来源：** `agent/middlewares/context_eviction/core.py`、`agent/middlewares/message_persistence/core.py`、`agent/middlewares/message_persistence/prepare.py`、`pub/func/message/eviction.py`、`pub/func/message/overflow_clip.py`、`pub/func/message/target_truncation.py`、`pub/func/message/tool_args_truncate.py`、`agent/middlewares/summarization/core.py`、`agent/middlewares/summarization/media_offload.py`、`pub/func/estimate_tokens.py`、`context_engine/store/core.py`、`config/features/agent_side/tool_result_eviction.py`、`config/features/agent_side/summarization.py`、`config/features/agent_side/token_estimation.py`。以下每条结论均已与上述代码逐条核对。

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
| 计划知识 | 非消息 —— 磁盘目录 | — | plan extraction | `workspace/knowledge/plans/<plan_key>/` | 按 `plan_ref` 注入 `<knowledge>` 块 |
| FACTS.md | `workspace/memory/FACTS.md`（memory 工具 target `facts`） | — | 跨计划、不绑定模块的坑与约定：压缩时记忆回顾 + 计划完成抽取 | memory 文件（1 375 字符上限；超限先淘汰最旧条目） | 作为常驻 FACTS 记忆块注入每次系统提示词 |

## 💾 逐边界持久化

`agent/middlewares/message_persistence/core.py`（`MessagePersistenceMiddleware`）是整页依赖的持久化底座。它在**两个时机**落库：

| 消息 | 落库时机 |
|---|---|
| `ToolMessage` | **工具处理器返回的瞬间**（`wrap_tool_call` / `awrap_tool_call`） |
| `HumanMessage` | 本回合的第一个模型边界（`after_model` / `aafter_model`） |
| `AIMessage`（含其 `tool_calls`） | 模型产出它之后的下一个边界 |
| HITL 拒绝（`status="error"` 的 ToolMessage） | 下一个模型边界 —— HITL 从外侧包裹本中间件，其短路永远到不了这层刷盘 |

两个时机共享同一条批处理管线：角色/标记过滤 → 水位 → HITL 拒绝消息重新配对 + 工具结果去重 → 写入 → 打标。值得注意的细节：

- **`persisted_message_ids` 水位（写一次）。** 写入前，`filter_persisted_message_ids` 丢弃查找键已在会话墓碑中的候选；写入后，`mark_message_ids_persisted` 为交给写入器的每个候选打上墓碑。水位是 SQLite 表（`context_engine/store/db.py`），跨重启存活。
- **id + 指纹双重查找。** 工具结果在返回时落库，**此时图 reducer 还没给它分配 id**；因此水位查找同时检查 LangGraph 消息 id 与 `sha1:` 内容指纹（role + content + `tool_call_id`）。下一个边界以及重启后的重放，都能落到两个键之一上（`prepare.py` 的 `_watermark_lookup_keys`）。
- **摘要对永不落库。** `_is_persistable` 丢弃所有 `additional_kwargs["lc_source"] == "summarization"` 的消息（对的 AI 半边），`HumanMessageRowBuilder.build`（`context_engine/store/core.py`）对人类半边返回 `None`。压缩产物是提示脚手架，不是对话历史。
- **失败开放。** 缺失 `session_id` 时静默跳过；写入器出错只记日志、**不**打墓碑，同一批在下一个边界重试。持久化永远不会弄坏一个回合或一条工具结果。

这正是本页后续机制可以如此激进的原因：**在任何后续阶段压缩它之前，每条载荷都已经持久化了。**

## 🗜️ 工具结果驱逐（P0-2）

`ContextEvictionMiddleware.wrap_tool_call` 在工具响应**进入图 state 之前**将其拦截（`agent/middlewares/context_eviction/core.py`；原语在 `pub/func/message/eviction.py`）：

- 提取文本**超过 `evict_threshold_chars`（20 000 字符）**的通用结果写入 `SESSIONS_DIR/<session_id>/evicted/<tool_call_id>_<md5[:8]>.txt`，消息内容替换为携带文件路径的 head/tail 预览。
- `excluded_tools`（8 个名字 —— `read_file`、`write_file`、`patch_file`、`search_files`、`list_files`、`memory`、`skill_view`、`skill_list`）原样通过：它们的载荷已在后端文件系统上或取回成本极低。`read_file` 另外走下面的切片路径。
- 替换经 `ToolMessage.model_copy` 构造，消息 `id`、`tool_call_id`、`name`、`status` 与 `additional_kwargs` 全部存活 —— 配对、净化器与水位始终匹配同一条逻辑消息。
- 多模态非文本块（图片/音频/视频）原样保留，只替换文本块。
- 安全护栏：不安全的 `session_id`（空 / `.` / `..` / 含分隔符）跳过且不碰磁盘；已带 `[evicted to: …]` 标记的消息绝不二次驱逐；预览不会比原文更小（单行巨行）时跳过。

预览格式（`pub/func/message/eviction.py`，`preview_head_lines = preview_tail_lines = 5`）：

```text
[evicted to: <path>]
--- head (5 lines) ---
<first 5 lines>
...
--- tail (5 lines) ---
<last 5 lines>
[full content: N chars, evicted at <ts>]

Use read_file(file_path='<path>', offset=0, limit=100) to read the full content in chunks.]
```

**三态存储** —— 驱逐后每份副本所在之处：

| 位置 | 内容 |
|---|---|
| 图 state / checkpointer / 下一次模型调用 | **仅预览** |
| MesMemory（`messages` 表） | **全文**，由内层持久化在工具返回瞬间写入 |
| `SESSIONS_DIR/<session_id>/evicted/` | 字节级一致的副本；`load_evicted()` / `read_file` 可取回 |

让这套划分成立的顺序：在 `wrap_tool_call` 链中，`MessagePersistenceMiddleware` 处于**最内层**，`ContextEvictionMiddleware` 位于其**外侧**，因此原始结果先落库，只有预览继续进入 state。水位两种情况都覆盖：`model_copy` 携带内层刷盘的进程内 `_db_persisted` 标记；当原始写入成功时，替换消息的水位键还会被额外打上墓碑（`_cover_with_watermark`）—— 这覆盖了标记丢失、预览指纹又与原始内容不再匹配的重启场景。当原始写入失败时，什么都不打墓碑，下一个边界以预览内容重试。

## ✂️ `read_file` 切片（P2-4）

`read_file` 的结果**不**卸载 —— 文件本来就在磁盘上，再写一份纯属重复。`slice_read_file_result`（`pub/func/message/eviction.py`）则把内容替换为**前 `_READ_FILE_SLICE_CHARS`（4 000）个字符**加一条恢复提示（`"...[Output was truncated due to eviction threshold. Use read_file with offset and limit to retrieve specific portions.]"`）。不写驱逐文件，且该辅助函数幂等：已切片的（含提示的）结果原样返回。

这是两级压缩中的**执行期**一半。**压缩期**一半位于 `pub/func/message/target_truncation.py::_truncate_read_file_content`：压缩裁切上下文时，它按 `tool_call_id` 把每条 `ToolMessage` 解析回其 `read_file` 调用，保留 `max_tool_output_chars`（2 000）的 head 30% + tail 30%，中间替换为携带**解析器推导的 1-based 续读偏移**的恢复提示（`Use offset=<N> to continue reading…`；载荷无法解析时退化为"从头重读"）。

两级在构造上互补：压缩期后续裁切一条已被执行期切片的载荷时，截断的 JSON 不再可解析，压缩提示因此确定性地回退为重启形式 —— 永远不发出错误偏移，执行期切片辅助函数也绝不二次切片自己的输出。

## 📥 人类消息驱逐（P1-9）

用户可能粘贴任何工具都产不出的载荷：日志、文档、转录稿、整个代码库。`ContextEvictionMiddleware` 移植了 DeepAgents 的人类消息驱逐，并做了 Sherry 特有的划分。

- **触发**（`before_model` / `abefore_model`）：`human_evict_enabled` 为真，且**最后一条**消息是 `HumanMessage`，且不带 `lc_evicted_to`，且其提取文本**超过 `human_evict_threshold_chars`（200 000 字符）**。只检查最后一条，因此过去的用户回合永远不会被重新检视。
- **打标 + 卸载**（`evict_human_message`）：全文写入 `SESSIONS_DIR/<session_id>/evicted/human-<msg_id|timestamp>.md`，然后钩子返回偏量 state 更新 `{"messages": [tagged]}`，其中 `tagged` 的 **id 与内容不变**，只新增 `additional_kwargs["lc_evicted_to"]`。标准 `add_messages` reducer 按 id **原地更新**该消息 —— 不重写消息列表、不依赖 `DeltaChannel`、state 侧不触发前缀缓存失效。文件先于标记写入，因此写失败永远不会留下悬空指针。
- **模型视图**（`wrap_model_call` / `awrap_model_call`）：每条携带 `lc_evicted_to` 的 `HumanMessage` **仅在请求中**（`request.override(messages=...)`）被替换为基于 state 文本构建的预览：驱逐路径、head/tail 各 5 行、以及 `read_file` 恢复提示。非文本块（图片/音频/视频）原样保留（`_build_evicted_content`）—— 媒体永远不会被卸载进文本文件。
- **自愈**：驱逐文件缺失时（会话目录被清、磁盘问题），`_heal_eviction_file` 会在下一次模型调用时从 state 文本重写它 —— 但仅当目标位于该会话自己的 `evicted/` 目录；标记中的外来路径会被拒绝并告警。

### 为什么与工具结果采用相反的三态划分

| 位置 | 工具结果（P0-2） | 人类消息（P1-9） |
|---|---|---|
| 图 state / checkpointer | 仅预览 | **全文** + `lc_evicted_to` 标记 |
| MesMemory（`messages` 表） | 全文 | **全文**（标记不过滤持久化） |
| 下一次模型调用（仅请求视图） | 预览 | 预览（路径 + `read_file` 提示） |
| `SESSIONS_DIR/<session_id>/evicted/` | 字节级一致副本 | 字节级一致副本 |

不对称源于消息**何时**落库。工具结果在工具返回时由内层持久化刷盘，所以 state 可以安全地只留预览。人类消息在回合的第一个 `after_model` 边界落库 —— 若 state 只留预览，MesMemory 就会归档预览，`message_search` 与压缩都会失去真实文本。因此 state 保留全文，只截断请求视图。由于消息 id 从不改变，水位不受影响，也不会写出第二行。

## 🖼️ 媒体治理（卸载、引用与 token 分型）

压缩与 token 估算都必须处理多模态载荷；两者都绝不能把 base64 当作文本。

**压缩期卸载。** 在 `_apply_compression` 序列化被摘要的前缀（`current_messages[:cutoff]`）之前，`offload_inline_media`（`agent/middlewares/summarization/media_offload.py`）只重写该范围内的每个内联媒体块：

- `data:` URL / base64 载荷（`image_url` / `audio_url` / `video_url`、裸 `base64` 字段、`audio_bytes` / `video_bytes`，或 Anthropic 风格的 `source.data`）被解码并写入 `SESSIONS_DIR/<session_id>/media/{sha256(raw)[:16]}{ext}`；扩展名由魔数经 `media_handlers._infer_extension` 推导；
- 相同字节只写一份 —— 以内容 hash 命名的文件名在单次及多次压缩之间去重；
- 该块变成文本指针 `[evicted to: <path>]`，与 P0-2 / P1-9 驱逐路径发出的标记一致，因此 `_collect_evicted_refs` 能收集到它，`SummaryDoc.evicted_refs` 会把路径带到摘要链上（渲染为 *Evicted References*）；
- 无法解码或写入的块变成 `<media error="failed_to_offload" />` —— fail-open，绝不崩溃。

保留窗口保持其媒体不动。两条压缩路径都会对前缀切片调用卸载 —— `_apply_compression_under_lock`（同步）与 `_aapply_compression_under_lock`（异步）。摘要提示词携带媒体引用规则：原样保留指针、不得臆测视觉/音频/视频细节、按路径取回载荷。媒体文件位于会话目录树内，因此 `clear_session()` 会把它们随 `evicted/` 与 `plans/` 一并删除。

**token 分型。** `pub/func/estimate_tokens.py` 对 content **列表**逐块计数：文本块按文本，媒体块按 `TOKEN_ESTIMATION` 的固定分型成本（`tokens_per_image_block = 85` —— 与 `langchain_core.count_tokens_approximately` 对齐；`tokens_per_audio_block = 256`；`tokens_per_video_block = 1024`；未知块取 `tokens_per_unknown_block = 85`，包括藏有 `data:` 载荷的块）。base64 绝不被序列化、绝不被计入文本：被移除的 JSON 路径会把 5 MB base64 读成约 125 万 token，单张图片就会触发压缩。`str` content、`None` 与纯文本列表保持其文本估算。

## ⚡ 溢出尾部裁剪（P1-2）

最新的工具输出通常既是最大的上下文消耗者，也最可牺牲 —— 而且每一条都已持久化。`pub/func/message/overflow_clip.py::clip_overflow_tail` 把它变成**每条溢出路径上的第一个、零 LLM 动作**：

- **先于路由。** `Summarization._dispatch_overflow_route`（T1/T2/T3）对每个非 `fits` 路由在执行前先跑裁剪；`_forced_recovery_request`（T4/T5）在强制压缩步骤之前先跑裁剪。当裁剪**单独**把估算压到线下时，请求带着 stub 列表返回，路由根本不执行 —— 不做预算截断、不调辅助 LLM。
- **裁剪方式。** 它从尾部向前遍历**连续的 `ToolMessage` 批次**（遇到第一条非 `ToolMessage` 或已 stub 的消息即停），把每条内容经 `model_copy` 替换为紧凑 stub。没有任何消息被删除、重排或注入：`id`、`tool_call_id`、`name` 与 `additional_kwargs` 全部存活，工具调用/结果配对与持久化水位保持不变。
- **标记存活。** stub 会原样重新携带 P0-2 的 `[evicted to: …]` 指针（外加 `read_file` 提示）与 P2-4 切片通知，因此裁剪之后恢复路径依然可用。
- **幂等。** 一条 stub 会终止可扫描批次，第二次遍历即 no-op —— T4/T5 重试预算不会被相同裁剪烧掉，下一次尝试退化为压缩。
- **有界。** `overflow_clip_max_remove`（10）限制一次裁剪的 stub 条数上限；`overflow_clip_min_keep`（5）让短转录不裁剪；`overflow_clip_enabled` 是总开关。

### 零 LLM 快路径 vs. 降级路径

| 情形 | 执行内容 | LLM 调用 |
|---|---|---|
| 裁剪单独把估算压到阈值之下 | 返回 stub 列表；路由不执行 | **0** |
| 裁剪不足（或被禁用 / 无可裁批次） | 丢弃裁剪结果；既有路由在**原始列表**上运行 | 视路由而定（compact 类路由会调辅助 LLM） |
| T4/T5 提供商错误，首次恢复尝试 | 先裁剪；足够则带着 stub 列表重试提供商调用 | **0** |
| T4/T5 提供商错误，裁剪不足 | 强制压缩 + 预算截断，然后重试 | 每个 compact 步骤 1 次辅助 LLM 调用（≤ `MAX_OVERFLOW_RETRIES = 3`） |

接受条件很严格：仅当裁剪**单独**把纯本地估算压到线下时才采用（`estimate_messages_tokens(messages, reported_tokens=0)` —— 陈旧的 `usage_metadata` 永远不会驱动恢复）。不足的裁剪整份丢弃，既有路由在原样列表上运行。

## 🧵 链式摘要过滤

一次压缩用一对带 `lc_source="summarization"` 标记的 `HumanMessage` / `AIMessage` 替换历史（AI 半边在 `<summary>` 标签内携带摘要）。下一次压缩时，把旧摘要再喂回序列化的 `<conversation>` 既浪费 token，也会让摘要器困惑。

`agent/middlewares/summarization/core.py` 分两步处理：

1. `_extract_previous_summary` 从转录中取出旧摘要文本（先找最新的带标记 `AIMessage`，再找带标记的 `HumanMessage`）。
2. `_filter_summary_messages` 从将要序列化进新提示的列表中移除**每一条** `lc_source="summarization"` 消息。`_build_summary_prompt` 把提取出的文本作为 `<prior-summary>` 单独注入，与更新指令并列；模型被告知要构造一份合并摘要，且旧摘要在此之后即被丢弃。

摘要对本身也永不进入 MesMemory（见[逐边界持久化](#-逐边界持久化)）—— 原始历史保持原始。

## 🔗 交互与顺序保证

顺序保证（已在 `agent/core.py` 核实，列表顺序 = 注册顺序）：

| 钩子阶段 | 此处关键的顺序 |
|---|---|
| `before_agent`（列表顺序） | `MultimodalProcessor` 在模型循环**之前**运行，因此 P1-9 打标总能见到媒体提示已合并的最终文本 |
| `before_model`（列表顺序） | P1-9 打标先于 `ToolCallNormalize` / `SubagentCompletionDrainMiddleware` |
| `wrap_model_call`（外→内） | ContextEviction（P1-9 视图替换）→ … → Summarization（最内层，最靠近 LLM） |
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
| `max_tool_output_chars` | `2_000` | 工具结果的压缩期裁切预算 |
| `content_head_ratio` / `content_tail_ratio` | `0.3` / `0.3` | 压缩期裁切的 head/tail 保留比例 |

`TOKEN_ESTIMATION`（`config/features/agent_side/token_estimation.py`），多模态 token 分型：

| 键 | 默认值 | 含义 |
|---|---|---|
| `chars_per_token` / `chars_per_token_cjk` | `4` / `2` | 文本估算除数（非 CJK / CJK） |
| `tokens_per_image_block` | `85` | 每个图像块的固定成本（与 `langchain_core.count_tokens_approximately` 对齐） |
| `tokens_per_audio_block` / `tokens_per_video_block` | `256` / `1024` | 保守固定成本（估算时无时长元数据） |
| `tokens_per_unknown_block` | `85` | 未识别块的固定成本 —— 绝不是它的 base64 |

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
| `tests/context_engine/store/test_persisted_message_ids.py` | `persisted_message_ids` 水位存储 |
| `tests/full/test_context_governance_e2e.py` | 实网 e2e（真实 LLM + 真实图）：六项机制端到端 —— 显式运行 |

```bash
# Hermetic suites (CI gate)
uv run pytest tests/agent/middlewares/context_eviction \
    tests/agent/middlewares/message_persistence \
    tests/pub/func/message/test_eviction.py \
    tests/pub/func/message/test_read_file_slice.py \
    tests/pub/func/message/test_overflow_clip.py \
    tests/agent/middlewares/test_summarization_overflow_clip.py \
    tests/agent/middlewares/test_summary_message_filtering.py \
    tests/agent/middlewares/test_compression_media_offload.py \
    tests/pub/func/test_estimate_tokens_media.py \
    tests/context_engine/store/test_persisted_message_ids.py -q

# Live-network e2e — RUN EXPLICITLY, never part of the CI gate
uv run --no-sync pytest tests/full/test_context_governance_e2e.py -v
```

## 🧹 清除语义与局限

- **`clear_session` 一次清除全部。** `server/DAO/messages.py::clear_session` 删除该会话的 MesMemory 行（连同 `persisted_message_ids` 水位一起删除）、checkpointer 历史，以及整个 `SESSIONS_DIR/<session_id>/` 文件夹 —— 因此 **`evicted/` 文件与 `plans/` 随会话一并删除**（`config/path.py::session_plans_dir` 记录了同样的整目录契约）。内存寄存器最后清理。
- **驱逐只是模型视图压缩，绝不是删除。** 本页压缩的每份载荷要么归档在 MesMemory、要么全文留在图 state（人类消息）、要么在磁盘 `evicted/` 下 —— 且每份预览都带指针。
- **`evicted/` 目录归会话所有，但没有垃圾回收。** 文件活到 `clear_session` 为止；没有逐消息 TTL。长会话 + 大量巨型工具结果会在 `workspace/sessions/<session_id>/evicted/` 下持续累积磁盘占用。
- **单行巨行永不驱逐。** 当 head 与 tail 都会包含整份载荷时，预览不可能比原文更小，消息原样保留（工具路径与人类路径都是如此）。
- **`read_file` 切片不可从切片自身恢复 —— 这是设计。** 源文件才是恢复路径；提示明确告诉模型如何续读。只有文件缺失/被删才会失效。
- **人类消息驱逐仅限尾部消息。** 巨型载荷之后如果还有另一个用户回合，它不会被重新检视；"只看最后一条"是刻意设计，避免重翻已定历史。
- **尾部裁剪只有在尾部批次够大时才有用。** 若上下文被人类回合或非工具消息占满，则退化为既有路由；P1-2 是优化，不是保证。
- **子代理转录不在范围内。** 子代理保留完整工具结果（不驱逐、不持久化）—— 其转录只存在于 checkpoint，永不进入客户端可见的 MesMemory 历史。
