# 🖼️ 媒体流水线：压缩、原生读取、历史预算、Artifact 与图片生成

[English](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> Agent 如何端到端处理图片与 PDF：每次上传都先经过同一条确定性压缩级联再发给 provider；`read_file` 可以让模型直接看到图片或 PDF，而不是绕道 skill；历史媒体按字节预算从新到旧保留，而不是每轮无条件剥离；持久化上传进入 SHA-256 内容寻址的 artifact 存储，相同字节自动去重，并通过 `sherry-artifact://` 引用提供服务；`image_generation` 则把提示词变成图片，存进同一个 artifact 存储。

事实来源：`pub/func/media/image_compress.py`、`agent/middlewares/media_pipeline/core.py`、`agent/middlewares/media_pipeline/budget_projection.py`、`agent/middlewares/media_pipeline/media_handlers.py`、`agent/middlewares/media_pipeline/tool_media.py`、`agent/middlewares/media_pipeline/scrub.py`、`agent/tools/file_tools/read_file.py`、`agent/tools/pub_base/pdf_render.py`、`agent/tools/pub_base/artifact_store.py`、`agent/tools/image_gen.py`、`models/TTI_model/core.py`、`agent/middlewares/summarization/media_offload.py`、`server/trigger/http/artifacts.py`、`client/app/composables/use-chat-media.ts`、`config/features/agent_side/media_pipeline.py`、`config/features/agent_side/image_gen.py`。本文档中的每个常量都已对照这些代码核实。

## 目录

- [概览](#-概览)
- [压缩：`prepare_for_model`](#-压缩prepare_for_model)
  - [预算门](#-预算门)
  - [级联](#-级联)
  - [接入点](#-接入点)
- [`read_file`：原生图片与 PDF](#-read_file原生图片与-pdf)
  - [图片](#-图片)
  - [PDF](#-pdf)
- [历史媒体预算](#-历史媒体预算)
  - [为什么不再全部剥离](#-为什么不再全部剥离)
  - [顺序与占位符](#-顺序与占位符)
- [内容寻址 Artifact](#-内容寻址-artifact)
  - [存储布局与去重](#-存储布局与去重)
  - [服务与渲染](#-服务与渲染)
  - [压缩期复用](#-压缩期复用)
- [图片生成](#-图片生成)
- [分块上传：`/upload/chunk`](#-分块上传uploadchunk)
- [配置参考](#-配置参考)
- [测试](#-测试)
- [限制](#-限制)

## 🎯 概览

媒体通过四个入口进入 Agent：用户上传（WebSocket / Tauri）、工具产生的截图、浏览器模式的 HTTP 上传，以及模型让 `read_file` 打开的文件。四个入口共用同一套压缩与能力判定机制。设计目标是向 ZCode 对齐：用逐图预算门压缩，让支持视觉的模型直接看到像素，在字节预算内保留历史媒体，并且绝不把模型读不了的内容递给 provider。

三条性质是关键：

- 压缩门是发送决策，不是上下文窗口估算器；压缩时机的权威仍是 provider 上报的 `usage_metadata.input_tokens`。
- 能力判定与预算判定彼此独立。清洗层回答“这个模型能不能读这个族别”；预算层回答“历史媒体还能放下多少”。
- 持久化字节按内容寻址。相同上传映射到同一个文件，引用可跨重启存活，后续压缩不会再写第二份副本。

## 🗜️ 压缩：`prepare_for_model`

`pub/func/media/image_compress.py::prepare_for_model` 把 ZCode 的 Jimp 级联移植到 PIL。它返回 `CompressedImage`，携带字节、媒体类型、扩展名、尺寸和所选策略标签。

### 🚦 预算门

一个候选必须同时满足三个条件才会被接受：

- 最长边不超过 `image_compress_max_dimension`（默认 `2000` 像素）；
- 原始字节数不超过 `max_base64_bytes * 3 / 4`（5 MiB base64 → 3.75 MiB 原始字节）；
- base64 token 估算 `ceil(ceil(raw_len / 3) * 4 * 0.125)` 不超过 `image_compress_max_tokens`（默认 `25000`）。

`0.125` 比率来自 ZCode 的 `Cde`。它刻意保守：对已知 provider 公式都高估，因此通过门的候选一定小于 provider 的真实要求。该门逐图计算，永不累计；一个会话仍可能因图片过多而溢出，`ContextLimitGuard` 会用 provider 上报的 token 数反应式兜底。

### 🪜 级联

1. **WebP 直通**（文件已达标时）；否则走 PIL WebP quality 80 → 60 → 40 → 20，最后回退 JPEG。（ZCode 的 Jimp 不能写 WebP，PIL 可以。）
2. **原图直通**（文件达标且最长边未超尺寸上限时）。
3. **保持格式重编码**：PNG `optimize=True`；JPEG quality 80 → 60 → 40 → 20；GIF 保持。
4. **缩放**到尺寸上限（LANCZOS），再重复保持格式与 JPEG 梯度。
5. **降采样梯度**：最长边的 75%、50%、25%，每步重试同样的候选。
6. **硬 JPEG 兜底**：quality 20，尺寸 1000、800、600、400、300、200 像素。
7. 全部失败时抛 `ImageTooLargeError`；调用方保留文本 / skill 路径。

只要 PNG 候选仍达标，就不会转成 JPEG，因此常见路径保留透明通道。结果上的策略标签（`original`、`png-optimized`、`jpeg-q60`、`resized`、`hard-jpeg-q20` 等）会出现在测试与日志里。

### 🔌 接入点

- `server/service/messages.py::_get_content_list` 在构造 `data:` URL 前压缩 `image_base64_list` 与 `image_bytes_list`。
- `agent/middlewares/media_pipeline/tool_media.py::_image_block` 在嵌入截图前压缩。
- `server/trigger/http/image.py::upload_image` 在写盘前压缩 HTTP 上传。
- `pub/func/media/image.py::download_and_convert_to_base64` 压缩 image-to-text skill 下载到的字节。

`image_compress_enabled`（默认 `true`）保留旧的字节原样直通。无法压进预算的图片在工具路径上会被省略，在 HTTP 上传路径上返回 `413`；用户上传路径保留原图并交给 provider 拒绝，而不是静默丢失附件。

## 📖 `read_file`：原生图片与 PDF

`read_file` 不再把图片当作不可读的二进制资源，也不再强制交给 `image_to_text` skill；它同时理解 PDF。两个分支都返回工具消息的 CONTENT 列表，而不是 JSON 包裹的字符串，因为只有列表才能让 `image_url` / `document` 块成为真正的多模态输入。

### 🖼️ 图片

该分支在文本与整文件逻辑之前运行，覆盖 `.png`、`.jpg`、`.jpeg`、`.gif`、`.webp`、`.bmp`、`.tiff` 与 `.tif`。文件受 `read_image_max_bytes`（20 MiB）限制，经级联压缩后写到 `TEMP_DIR/read_images/<sha256[:16]><ext>`，再通过 `native_image_content(session_id, text, saved_path)` 返回：

- 支持视觉的会话得到 `[{"type": "text", …}, {"type": "image_url", …}]`，与 `browser_screenshot` 完全同一条机制；
- 已知不支持视觉的会话只得到文本，并附带一行提示，让它在原始路径上使用 `image_to_text` skill；
- 无法解码的文件返回 `read_image_failed`；超限文件返回 `read_image_input_too_large`。

文本会写明原始路径与压缩后的尺寸，因此 skill 回退仍有可读文件。`ContextEviction` 的 `read_file` 切片器刻意跳过块列表结果：其文本已由工具自身预算，切 `str(content)` 会把整段 base64 嵌进去再把媒体块切掉。

### 📄 PDF

PDF 分支依据服务模型的声明能力选择路径（会话覆盖优先于环境模型）：

- 支持 `pdf` 且未请求页面范围 → 原始文档作为原生块：Anthropic 用 `{"type": "document"}`，OpenAI 兼容 provider 用 `{"type": "file"}`。
- 不支持 `pdf` 但支持 `vision` → 通过 `agent/tools/pub_base/pdf_render.py` 渲染成 JPEG 页面，逐页压缩后以 `image_url` 附加。
- 支持 `pdf`、请求了页面范围、且支持 `vision` → 同样走页面渲染。
- 支持 `pdf`、请求了页面范围、但不支持视觉 → `pdf_pages_images_unsupported`。
- 两个族别都不支持 → `pdf_not_supported`。

`render_pdf_pages` 优先使用已安装的 PyMuPDF，否则使用 poppler 的 `pdftoppm`；两者都不是硬依赖。它以 `pdf_render_dpi`（100）渲染，单次上限 `pdf_max_pages_per_request`（20）页、`pdf_render_timeout_s`（120 秒），无后端或外部渲染失败时报告 `pdf_render_unavailable`。`pdf_max_file_bytes` 把输入限制在 20 MiB。

## 🧮 历史媒体预算

旧行为在 `before_agent` 里剥离除最后一条之外所有消息的 `image_url` 块，导致“图片轮之后的文本轮忘记图片”，也与“让模型看到像素”的方向相反。该剥离已删除。

### 🧭 为什么不再全部剥离

现在每次请求携带尽可能多的历史媒体，上限是 `history_media_budget_bytes`（75 MiB，对应 ZCode 的 `Mui`），从新到旧选择。由于上传已被压缩到几百 KB，正常会话会保留全部图片；预算只在未压缩旧文件、关闭压缩或超长图片会话中起作用。

### 🛡️ 顺序与占位符

`MediaBudgetProjectionMiddleware`（`agent/middlewares/media_pipeline/budget_projection.py`）在 `wrap_model_call`（及其异步版本）里执行 `_project_media_budget`；`MultimodalProcessor` 保留上传 / 持久化 / 能力清洗那一半，并先执行 `_scrub_request`。清洗层把模型读不了的族别替换成文本占位符；预算中间件随后按解码后的 data URL 字节数计量每个存活的 `image_url` / `audio_url` / `video_url` 块（远程 URL 由 provider 自行抓取，计 0 字节），并构造 `model_copy` 请求副本。检查点永不被改写。该中间件注册在 main 与 child 两条链上，因此子代理读取大量图片时与主 Agent 使用同一套预算。

最新一条真实用户消息（`metadata.origin == "user"`）受保护：即使它单独就超出预算，其媒体也会保留。没有该标记的记录会回退到最新一条带媒体的 `HumanMessage`。受保护字节先从预算中扣除，其余图片按消息下标从新到旧接纳。放不下的块变成：

```
[Attached image]
[Media omitted from provider request to keep the request body under the configured media budget.]
```

## 🗃️ 内容寻址 Artifact

### 📦 存储布局与去重

上传图片由 `agent/tools/pub_base/artifact_store.py` 持久化。名字是 `<family>-<sha256[:16]><ext>`，目录是 `SRC_DIR/<session_id>/artifacts/`，引用是 `sherry-artifact://<session_id>/<name>`：

```
src/<session_id>/artifacts/
└── image-3c1bdb09e4e6b674.png
```

相同字节与族别的第二次写入会在进程锁下的存在性检查中被跳过，因此重试与重复上传不会让存储增长。session id 同时属于路径与 URI：删除会话即删除其 artifact，一个会话也永远解析不到另一个会话的名字。写入是原子的；存储失败返回 `None`，调用方保留旧路径。

### 🌐 服务与渲染

`GET /artifacts/:session_id/:name`（`server/trigger/http/artifacts.py`）校验两段参数，在该会话的 artifact 目录内解析文件，并按扩展名返回 Content-Type。非法 session 或名字返回 `400`；不存在或跨会话的名字返回 `404`。

客户端在 `use-chat-media.ts` 中把 `sherry-artifact://` 解析为 `/artifacts`（`resolveImageSrc` 与 `resolveMediaSrc` 都处理），并且该分支排在文件系统扩展名判断之前，因为名字虽然以 `.png` 结尾却并不是路径。Markdown 图片同样处理：`ChatBox.vue` 在 Markdown 渲染前用 `resolveArtifactRefsInMarkdown` 改写回答，因此 AI 可以写 `![](sherry-artifact://<sid>/<name>)`。

### 🔁 压缩期复用

压缩卸载内联媒体时，`media_offload` 先用解码后的字节与媒体族别调用 `find_artifact`。命中说明上传路径已存过这份内容，该块直接变成 `[evicted to: <artifact path>]` 指针，不再写第二份；未命中则保留旧的解码 + 写入路径（`SESSIONS_DIR/<session_id>/media/`），也覆盖音频、视频字节等非流水线媒体。

## 🎨 图片生成

`image_generation` 是主 Agent 的文本生成图片工具。只有当 `TTI_*` 家族配置完整（`TTI_API_NAME` + `TTI_API_KEY`；`TTI_MODEL_PROVIDER` 与 `TTI_API_BASE` 指向一个 OpenAI 兼容的 Images API）时才会注册；它不会出现在子代理里——生成要花钱，且用哪个模型是操作者的选择。

provider 的一半在 `models/TTI_model/core.py::TTIModel`。它发送 `POST /v1/images/generations`，携带 `model`、`prompt`、`n`、`size`；`quality` 只在适用时发送（gpt-image 模型会丢弃旧的 `standard`/`hd` 值）；较旧的 `dall-e-*` 强制 `response_format="b64_json"`，`gpt-image-*` / `chatgpt-image-latest` 则省略；新模型专属选项（`background`、`output_format`、`output_compression`、`moderation`）只发给新模型，DALL·E 的 `style` 只发给旧模型。URL 响应会被下载（50 MiB 上限）并当作 `b64_json` 同样处理。

图片数量有两层钳制：每模型上限 `IMAGE_GEN_MAX_OUTPUT`（`dall-e-3` = 1，`dall-e-2` 与 gpt-image 家族 = 10，未知模型 = 1），再加 `IMAGE_GEN["max_n"]`（10）作为硬上限。每张返回图片都通过 `write_artifact(session_id, data, media_type="image", ext=…)` 写入，因此相同字节去重、每会话 URI 稳定；工具返回与已存上传完全一致的 Markdown 引用：

```
Generated 2 image(s) with gpt-image-1:

![generated image 1](sherry-artifact://<session>/image-<sha256[:16]>.png)

![generated image 2](sherry-artifact://<session>/image-<sha256[:16]>.png)
```

聊天既有的 `resolveArtifactRefsInMarkdown` + 画廊/lightbox 路径会渲染最终回答里的引用；**工具结果**现在也走同一条路：`ChatToolCard` 从任意工具结果中提取 `sherry-artifact://` 引用，用 `ArtifactImageGallery` 缩略图网格替代纯文本 `<pre>`（折叠状态下也显示网格，生成的图片不会藏在折叠卡里；点击打开共享的 `useImagePreview` 画廊）。折叠的 turn 也会提示隐藏的图片：`classifyTurnFold` 收集工具行里的引用，`ChatTurnHistoryStatus` 在展开按钮旁最多显示 4 张缩略图。没有生成专用的渲染器——分支只认 URI，不认工具名。

同一个 provider 也支撑 `text_to_image` skill 的 terminal 脚本（`skills/builtin/text_to_image/scripts/core.py`）。terminal 工具向每个子进程环境注入 `SHERRY_SESSION_ID`（同步、异步与后台 spawn 都是），脚本通过 `TTIModel` 生成、用 `write_artifact` 存储、把每个 URI **打印到 stdout**，失败输出 `[error] …`；旧脚本什么也不打印、写到 `STATIC_DIR/images`，agent 根本不知道图在哪。skill 的纯 URI 行与专用工具的 markdown 引用走同一个缩略图分支。provider 报错、缺少 key 或 artifact 写入失败都返回 `Error: …` 文本（工具）或 `[error] …` stdout 行（脚本），不会让异常逃出工具；工具侧的 provider 调用在工作线程中执行，不阻塞事件循环。

阶段 1 只做生成。`inputImageMask` / `inputFidelity`（inpaint）与流式 partial image 事件都未接线；模型能力表（`config/model_capabilities.json`）描述的是输入媒体，因此图片生成模型的上限放在 `models/TTI_model/core.py::IMAGE_GEN_MAX_OUTPUT`，不会作为假的输入能力塞进能力表。

## 📤 分块上传：`/upload/chunk`

浏览器与 Tauri 外壳通过 `bridge/upload-chunked.ts` 发送附件。不超过 `CHUNK_SIZE`（512 KiB）的载荷继续走整包 `/images|/audio|/video/upload`。更大的载荷先用 `POST /upload/begin`（session、文件名、内容类型、`total_chunks`、`total_bytes`）开启会话，再把每个分块以 `POST /upload/chunk?upload_id&chunk_index` 发送，最后调用 `POST /upload/complete?upload_id`。

单个文件的分块严格顺序：服务端拒绝跳号（`code=chunkGap`）与重发（`code=chunkConflict`），与 ZCode 一致。并行发生在文件之间——`uploadMultipleFiles` 通过 `runPool` 最多同时运行 `max_concurrent_files`（8）条车道，每条车道内部仍是顺序分块。`GET /upload/status` 返回连续已收分块数供 `resumeUpload` 续传，`POST /upload/abort` 删除暂存目录。

服务端把分块暂存在 `TEMP_DIR/upload_chunks/<upload_id>/` 并写入 `meta.json`；`begin` 校验按类型的字节上限以及分块数与字节数的匹配，`complete` 拼接、计算 SHA-256，并把 `<sha256[:16]><ext>` 写入整包路由使用的同一按类型目录。相同字节复用同一文件与 URL；完成的上传删除暂存，不完整的上传返回 `400` 并保留分块以便续传。`bridge/upload-cache.ts` 对载荷做 SHA-256，并在 Dexie `uploadCache`（schema v7，含活动 upload id 与状态）中保留 `read_cache_ttl_ms`（30 秒），窗口内重复发送不再上传；缓存失败开放。

## ⚙️ 配置参考

| 配置项 | 默认值 | 含义 |
|---|---|---|
| `image_compress_enabled` | `true` | 运行压缩级联；关闭则原样发送。 |
| `image_compress_max_dimension` | `2000` | 最长边像素上限。 |
| `image_compress_max_tokens` | `25000` | 逐图 base64 token 门。 |
| `image_compress_token_ratio` | `0.125` | 每个估算 token 对应的 base64 字符数。 |
| `read_image_max_bytes` | `20971520` | `read_file` 图片输入上限（20 MiB）。 |
| `pdf_max_file_bytes` | `20971520` | `read_file` PDF 输入上限（20 MiB）。 |
| `pdf_max_pages_per_request` | `20` | 单次调用页面渲染上限。 |
| `pdf_render_dpi` | `100` | 渲染页分辨率。 |
| `pdf_render_timeout_s` | `120.0` | 外部渲染器超时。 |
| `history_media_budget_bytes` | `78643200` | 保留的历史图片预算（75 MiB）。 |
| `preserve_latest_user_media` | `true` | 保护最新真实用户消息的媒体。 |
| `artifact_store_enabled` | `true` | 使用内容寻址存储；关闭则保留时间戳 `media/` 副本。 |
| `chunk_size_bytes` | `524288` | 单个 `/upload/chunk` 请求体的最大字节数。 |
| `max_concurrent_files` | `8` | 单次多附件发送的并行文件车道数。 |
| `read_cache_ttl_ms` | `30000` | 已上传 URL 的客户端去重窗口。 |
| `IMAGE_GEN["max_n"]` | `10` | 单次生成请求的硬上限（先应用每模型上限）。 |
| `IMAGE_GEN["default_size"]` | `"1024x1024"` | 调用方省略尺寸时的默认值。 |
| `IMAGE_GEN["default_quality"]` | `""` | 调用方省略质量时的默认值（空 = provider 默认）。 |
| `IMAGE_GEN["timeout_s"]` | `120.0` | provider 与图片下载超时。 |

## 🧪 测试

- `tests/pub/func/media/test_image_compress.py` — 直通、质量梯度、尺寸上限、token 门、WebP、`too_large`。
- `tests/agent/tools/file_tools/test_read_file_image.py` 与 `test_read_file_pdf.py` — 原生块、skill 回退、上限、页面范围错误。
- `tests/agent/tools/pub_base/test_pdf_render.py` — PyMuPDF 路径、`pdftoppm` 回退、超时与缺渲染器错误。
- `tests/agent/middlewares/media_pipeline/test_budget_projection.py` — 全部容纳、从新到旧淘汰、保护最新用户、远程 URL、全部丢弃、两条链。
- `tests/agent/tools/pub_base/test_artifact_store.py` 与 `tests/server/trigger/http/test_artifacts.py` — 去重、并发、URI 解析、服务、会话作用域。
- `tests/agent/middlewares/test_compression_media_offload.py` — artifact 复用与旧卸载路径。
- `tests/agent/middlewares/test_media_pipeline_e2e.py` — 真实 agent 图：服务端压缩 → artifact 存储 + `/artifacts` → `read_file` 图片/PDF 块 → 历史预算投影。
- `tests/server/trigger/http/test_upload_chunked.py` 与客户端 `upload-chunked` / `upload-cache` 套件 — 暂存、拼接、内容哈希去重、上限与短 TTL 缓存。
- `tests/models/TTI_model/test_core.py` 与 `tests/agent/tools/test_image_gen.py` — 请求形状、每模型上限、`response_format` 守卫、URL 下载、artifact 去重、错误文本与注册门。
- `client/app/composables/__tests__/use-chat-media.test.ts` — 附件与 Markdown 引用中的 URI 解析。

## ⚠️ 限制

- 页面渲染回退需要 PyMuPDF 或 poppler，两者都未随包分发；没有它们的部署会报告 `pdf_render_unavailable`，只能依赖原生 PDF 支持。项目没有把 PyMuPDF 加进 `pyproject.toml`，因为其许可证（AGPL）属于分发决策，而不是实现细节。
- 视频只做单载荷上限（`video_max_bytes`，30 MiB），不压缩，也没有累计历史桶；ZCode 同样如此。
- 75 MiB 预算是请求体大小启发式，不是 token 预测器：大量压缩图片仍可能消耗可观的 provider token，压缩中间件仍是反应式兜底。
- 在 `artifact_store_enabled` 关闭前写入的 artifact URI 仍可读取；新上传回退到时间戳 `media/` 文件与 `/media` 端点。
- 图片生成依赖操作者配置的 provider：没有 `TTI_API_NAME` 与 `TTI_API_KEY` 时工具根本不存在，模型无法画图。阶段 1 没有编辑 / inpaint，也没有流式预览。
