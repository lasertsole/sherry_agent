# 🗜️ 驱逐、媒体与溢出

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> [Context Governance](../README.zh.md) 的一部分：逐边界持久化底座、三条驱逐路径、`read_file` 切片、媒体治理、溢出尾部裁剪与链式摘要过滤。

---

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

媒体治理在入口与出口两侧同时进行：入口规则把无法处理的载荷挡在请求之外、让不可用的模型不被反复试探；压缩与 token 估算则绝不把 base64 当作文本。

**输入体积上限。** 每个入站媒体载荷 —— 内联 base64 / `data:` 块或远程 URL 下载 —— 都在**任何磁盘写入之前**按 `MEDIA_PIPELINE["max_media_bytes"]`（20 MiB）量度。超限载荷被跳过：不写盘、路径不进入 `MediaPaths`、warning 记录字节数，消息携带一行模型可见的 `[Uploaded media]` 说明该附件未保存、也未发送给模型。远程 URL 先看服务端声明的 `Content-Length`，没有则按 `limit + 1` 字节限量读取，因此错误的响应头无法迫使超限写入；图片、音频、视频处理器共用同一闸门（`media_handlers.py::_exceeds_media_limit` / `_record_oversize`）。恰好等于上限的载荷允许通过。

**每请求能力擦洗。** `MultimodalProcessor.wrap_model_call`（auto 模式）只重建请求副本 —— `request.override(messages=...)` —— 把服务模型缓存为 `"unsupported"` 的媒体族块逐一替换为文本占位，占位写明块类型、记录的落盘路径与对应内置技能（`image_to_text` / `speech_to_text` / `video_text_to_text`）。supported 与未探测的块原样通过，因此混合消息为受支持的族保留原生媒体、只剥离不受支持的块 —— 逐块进行。state、checkpointer 与 MesMemory 绝不写入；没有任何块需要替换时返回原请求对象。擦洗只在 `"auto"` 下运行：`"true"` 为模型保留全部块，`"false"` 在请求组装之前就走技能路径。

**静默降级检测。** 模型可能接受媒体块却当作从未看到。对于以 auto 模式原生尝试开始并成功返回的调用，`LLMRetryMiddleware` 用 `detect_media_blindness()`（`media_pipeline/degradation.py`）评估回复文本 —— 纯正则，en / zh / ja / ko，精确率优先：只有当盲区措辞 ±40 字符窗口内出现媒体词才计命中 —— 外加显式的「请描述媒体」请求模式。命中则把请求中实际出现的每个媒体族缓存为 `"unsupported"`，后续轮次直接走技能路径；无论命中与否都会清除本轮原生标志。`main_llm_silent_degradation_detection`（True）是总开关。归属跟随实际服务模型：`LLMRetryMiddleware` 把请求重绑到黏性回退候选时，先把本轮原生模型键改写为 `{candidate.provider}/{candidate.model_name}`，因此错误拒绝与静默拒绝都记在实际服务该调用的模型名下。

**能力缓存。** 三项入口行为共用一个进程级缓存（`agent/middlewares/llm_capability_cache.py`），键为 `"{provider}/{model_name}"`，逐媒体族取值 `"auto"`（未探测）/ `"supported"` / `"unsupported"`。它只存在于进程内：重启即清空（最多浪费一次原生探测），模型切换（改 env + 重启）天然产生新键。

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
