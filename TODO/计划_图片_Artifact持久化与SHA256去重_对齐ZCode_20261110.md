# 计划：Artifact 持久化 + SHA256 去重（对齐 ZCode）

- 日期：2026-11-10
- 状态：**待评审**
- 来源：ZCode 对齐 — ZCode 将工具结果二进制 artifact 写入内容寻址存储（`zcode-artifact://` URI），按 SHA256 去重，并在 markdown 中引用
- 风险等级：**中等** — 新增存储层；涉及工具媒体附件、媒体服务和 markdown 渲染
- 依赖：无（与压缩/token 计划独立）

---

## 0. ZCode 行为

### 0.1 Artifact 存储

ZCode 通过 `writeToolResultBinaryArtifact` 写入工具结果二进制 artifact（截图、生成图片、MCP 图片结果）：
- 路径：`<rootDir>/<sF(sessionId)>/<sF(toolCallId)>-<uuid><ext>`，其中 `sF` 将 id 净化为 `[a-zA-Z0-9._-]`，最长 120 字符
- 返回 `uri: "zcode-artifact://<enc(sessionId)>/<enc(id)>"`
- 根目录：按媒体类型分别为 `image-cache/`、`pdf-cache/`、`video-cache/`

### 0.2 派生媒体附件（内容寻址去重）

`writeDerivedMediaAttachment`（`YNt`）：
- 计算确定性路径：`<rootForType>/<sF(sessionId)>/<type>-<sha256(data).slice(0,32)><ext>`
- 若文件已存在（`QNt(path)`）→ 返回 `{status: "ready"}` 而不重写 — **按 SHA256 前缀内容寻址去重**
- `mediaAttachmentPathFlights` Map 对同一 URI 的并发写入去重

### 0.3 `zcode-artifact://` 协议

- `tLt(e)` 解析 `zcode-artifact://<sessionId>/<id>` URI
- 用于 markdown 中引用已存储图片：`![](zcode-artifact://...)`
- `d5t`（`extractMarkdownArtifactImageRefs`）用正则从 markdown 中提取这些引用，通过 `mapOutsideMarkdownFences` 应用以跳过代码块

## 1. Sherry 当前状态

**Artifact 存储已存在于 `media_offload.py` — 只需提取并泛化。**

### 1.1 `_store_media` — 已存在的 SHA256 内容寻址存储

**`agent/middlewares/summarization/media_offload.py:153-170`** — `_store_media(raw, family, session_id)`：
- 计算 `hashlib.sha256(raw).hexdigest()[:16]`（16 字符哈希前缀）
- 命名文件为 `<hash16><ext>`（扩展名通过 `media_handlers.py` 的 `_infer_extension` 推断）
- **按存在性检查去重**：`if path.exists(): return path` — 跳过相同字节的重写
- 写入 `SESSIONS_DIR/<session_id>/media/` 下
- 这正是 artifact 存储 — SHA256 内容寻址，按存在性跳过去重

### 1.2 支撑基础设施已就绪

| 机制 | 位置 | 复用 |
|---|---|---|
| `_extract_inline_media` / `_decode_data_url` / `_decode_base64` | `media_offload.py:100-150` | 将 `data:` URL / base64 / 原始字节输入归一化到存储 |
| `GET /media` 服务端点 | `server/trigger/http/media.py:37-73` | 已从 `<SRC_DIR>/<session_id>/media/` 提供服务并带路径遍历保护 — **复用为 artifact 服务端点** |
| `_infer_extension` | `media_handlers.py:54` | 魔数 → 扩展名，用于文件命名 |
| `EVICTION_PREFIX`（`[evicted to: <path>]`） | `pub/func/message/eviction.py` | 现有指针格式 — artifact URI 替代它 |
| `atomic_write_text_no_follow` | `agent/tools/pub_base/atomic_write.py` | 原子写入（`model_caps.py` 使用） |
| 用于 CAS 的全文件 SHA256 | `approval_store.py:234,250`、`ast_grep/provisioner.py:35` | 版本/完整性哈希先例 |

### 1.3 缺失部分（真正新增的）

- `sherry-artifact://` URI 方案（目前仅文件系统路径）
- 跨会话存储（当前存储按 session-id 作用域）
- 元数据索引（当前存储是裸文件系统 CAS，无 SQLite/JSON 注册表）

## 2. 设计

### 2.1 将 `_store_media` 提取为独立 artifact 存储模块

**Artifact 存储已作为 `media_offload.py::_store_media` 存在** — 将其提取到 `artifact_store.py` 并泛化：

```python
# agent/tools/pub_base/artifact_store.py
# EXTRACTED from agent/middlewares/summarization/media_offload.py:_store_media

def write_artifact(session_id, data, *, media_type, ext) -> str:
    """Write binary artifact to content-addressed store.
    REUSES the existing _store_media SHA256 + existence-skip pattern.
    Returns 'sherry-artifact://<session_id>/<hash>' URI.
    """
    digest = hashlib.sha256(data).hexdigest()[:16]  # same as _store_media
    path = _artifact_root(media_type) / _sanitize_id(session_id) / f"{media_type}-{digest}{ext}"
    if not path.exists():  # dedup by existence — same as _store_media
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return f"sherry-artifact://{session_id}/{digest}"
```

相对 `_store_media` 的关键变化：返回 `sherry-artifact://` URI 而非文件系统路径，并使根目录跨会话（不再作用域在 `SESSIONS_DIR/<sid>/media/` 下）。

### 2.2 复用现有 `/media` 服务端点

**`server/trigger/http/media.py:37` 已从 `<SRC_DIR>/<session_id>/media/` 提供服务并带路径遍历保护。** artifact 存储可以：
- 方案 A：将 artifact 存储在相同的 `media/` 目录下 → 原样复用 `/media` 端点
- 方案 B：存储在新的 `artifacts/` 目录下 → 新增一个轻量 `/artifacts/<sid>/<hash>` 路由（与 `/media` 相同的模式）

推荐方案 A — 零新增服务代码。

### 2.4 接入工具媒体和媒体 handler

用 `write_artifact()` 替换 `ImageUrlHandler.process` 和 `tool_media._image_block` 中基于时间戳的保存：
- 不再使用 `media/<ts><ext>`，而是写入内容寻址存储
- 返回 `sherry-artifact://` URI 而非文件系统路径
- `additional_kwargs["images"]` 列表存储 artifact URI 而非原始路径

### 2.5 Artifact 服务端点

新路由：`GET /artifacts/<session_id>/<hash>` — 解析并服务 artifact 文件。
- 校验 `session_id` 和 `hash` 格式
- 从内容寻址存储提供服务
- 与 `/media` 相同的 gateway auth/CORS

### 2.6 Markdown artifact 引用

更新 `use-chat-media.ts` 以解析 `sherry-artifact://` URI：
- `resolveImageSrc` 新增分支：若 src 以 `sherry-artifact://` 开头 → 解析为 `/artifacts/<sid>/<hash>` HTTP URL
- 这让 AI 消息能在 markdown 中引用已存储 artifact：`![](sherry-artifact://<sid>/<hash>)`

### 2.7 迁移

现有 `media/<ts><ext>` 文件仍可通过 `/media` 服务 — 无需迁移。新 artifact 进入内容寻址存储。

## 3. 变更清单

| 文件 | 变更 |
|---|---|
| `agent/tools/pub_base/artifact_store.py` | **新文件** — `write_artifact()`、`resolve_artifact()`、并发写入去重 |
| `agent/tools/pub_base/__init__.py` | 导出 artifact 存储函数 |
| `agent/middlewares/media_pipeline/media_handlers.py` | `ImageUrlHandler.process`：使用 `write_artifact()` 替代时间戳保存 |
| `agent/middlewares/media_pipeline/tool_media.py` | `_image_block`：使用 `write_artifact()` 持久化截图 |
| `agent/middlewares/summarization/media_offload.py` | **简化**：图片在摄入时即持久化到 artifact 存储后，`offload_inline_media` 不再需要在摘要过程中解码+写入 `data:` 媒体到磁盘 — 可直接将内联 `data:` 块替换为 `sherry-artifact://` URI 引用（artifact 已存在于磁盘）。移除解码/写入路径；仅保留 URI 替换路径。 |
| `server/trigger/http/artifacts.py` | **新文件** — `GET /artifacts/<session_id>/<hash>` 服务端点 |
| `server/trigger/http/__init__.py` | 注册 artifacts 路由 |
| `client/app/composables/use-chat-media.ts` | `resolveImageSrc`：将 `sherry-artifact://` URI 解析为 `/artifacts/` HTTP URL |
| `config/features/agent_side/media_pipeline.py` | 新增 `artifact_store_enabled: bool = True` |

### 3.1 media_offload 简化细节

**当前 `offload_inline_media`**（`agent/middlewares/summarization/media_offload.py:52-70`）：
1. 在被压缩的消息中查找内联 `data:` 媒体块
2. **解码** base64 负载为字节
3. **写入**字节到 `SESSIONS_DIR/<sid>/media/<sha256><ext>`（内容哈希文件名）
4. 用 `[evicted to: <path>]` 文本指针替换该块

**artifact 存储落地后：**
- 通过媒体流水线上传的图片已位于 artifact 存储中（摄入时经 `ImageUrlHandler.process` → `write_artifact()`）
- `offload_inline_media` 可完全跳过解码+写入步骤 — 直接用 `sherry-artifact://` URI 替换 `data:` 块（该 URI 在摄入时已记录在 `additional_kwargs["images"]` 中）
- 该函数从"解码 + 写入 + 替换"缩减为仅"用 URI 替换"
- `SESSIONS_DIR/<sid>/media/` 写入路径对于媒体流水线来源的图片变为死代码（保留作为非流水线来源的回退）

## 4. 测试

| 文件 | 测试 |
|---|---|
| `tests/agent/tools/pub_base/test_artifact_store.py` | 写入 → 返回 URI；相同字节 → 相同 URI，不重写；并发写入 → 去重；解析 → 正确路径；无效 URI → None |
| `tests/server/trigger/http/test_artifacts.py` | 有效 URI → 200 返回字节；无效哈希 → 404；跨会话 → 403 |
| `tests/agent/middlewares/summarization/test_media_offload.py` | **新增** — 媒体流水线来源的图片 → offload 用 `sherry-artifact://` URI 替换 `data:`（无解码/写入）；非流水线 `data:` 块 → 回退到解码+写入路径 |
| `client/app/composables/__tests__/use-chat-media.test.ts` | `sherry-artifact://` src → 解析为 `/artifacts/` URL |

## 5. 备注

- 本计划对齐 ZCode 的 `writeToolResultBinaryArtifact` + `writeDerivedMediaAttachment` + `zcode-artifact://` 协议。
- `summarization/media_offload.py` 已为压缩 offload 使用基于 SHA256 的文件名 — 该代码可重构以共享 artifact 存储。
- `sherry-artifact://` 协议名偏离 ZCode 的 `zcode-artifact://`（品牌差异），但机制完全相同。
