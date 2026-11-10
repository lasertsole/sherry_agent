# 计划：Read 工具读取图片（对齐 ZCode）

- 日期：2026-11-10
- 状态：**待评审**
- 来源：ZCode 对齐 —— ZCode 的 Read 工具直接读取图片文件，经 `prepareForModel` 压缩，并嵌入 `image_url` 内容块供模型查看
- 风险等级：**低-中** —— 向 `read_file` 新增图片分支；依赖压缩级联（`计划_图片token估算与压缩对齐ZCode_20261110.md`）
- 依赖：`计划_图片token估算与压缩对齐ZCode_20261110.md`（压缩级联必须先存在）

---

## 0. ZCode 行为

ZCode 的 Read 工具明确 "读取图片（PNG、JPG……）并以视觉方式呈现。" 当遇到图片文件时：
1. 读取原始字节
2. 通过魔法数检测 MIME（`bAr`：`.png→image/png`、`.jpg/.jpeg→image/jpeg`、`.gif→image/gif`、`.webp→image/webp`）
3. 调用 `imageProcessorPort.prepareForModel({data: content, maxBase64Bytes:G1, maxDimension:i4, maxRawBytes:iC, maxTokens:hg, ...})` —— 与各处使用的同一压缩级联
4. 返回 `image_url` 内容块，含 `data:<mime>;base64,<compressed>`，使模型直接看到图片
5. 硬上限：`Tde = 20MB` 原始 → 在任何处理前报错 `read_image_input_too_large`

模型的 `supportsImages` 标志对此门控：`if(n.type==="image" && e.supportsImages===!1) throw "Model does not support image input"`。

## 1. Sherry 当前状态

**`agent/tools/file_tools/read_file.py`** 不读取图片：
- `_reject_binary_and_device_paths`（239-266 行）拒绝 `_BINARY_EXTENSIONS`（归档、编译产物、数据库），但不包含图片扩展名（`.png`、`.jpg` 等）
- 图片文件通过校验器，被作为字节读取（`_read_bytes`），经 `sniff_text_encoding` —— 若编码嗅探判定为二进制则被拒绝；否则可能返回乱码文本
- `read_file` 中**不存在图片转 base64 / image_url 嵌入路径**
- 图片读取被委托给 `image_to_text` skill（`skills/builtin/core/image_to_text/`），该 skill 将图片发送至独立的 vision model（`ITTT_model`）并返回文本 —— 主模型从未看到图片像素

**这是架构分歧：** ZCode = 模型直接看到图片；Sherry = skill 转为文本。

## 2. 设计

### 2.1 向 `read_file` 新增图片分支

在 `read_file._core`（约 327 行），文件打开后、文本处理前，新增：

```python
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}

suffix = Path(file_path).suffix.lower()
if suffix in IMAGE_EXTENSIONS:
    return _read_image(file_path, session_id, offset, limit)
```

### 2.2 `_read_image` 实现 —— 复用 `native_image_content`

**关键：工具返回值必须直接是 `str` 或 `list[dict]`（content block 列表），不能是 dict 包裹的 JSON** ——否则会被序列化成字符串，`image_url` block 变成 JSON 文本而非多模态内容，模型看不到图片。

Sherry 已有现成机制：`tool_media.py::native_image_content(session_id, text, image_path)` ——浏览器截图就是用这个把图片放进 `ToolMessage` 的。它返回 `str`（模型不支持图片时）或 `[text_block, image_block]`（支持时），这个列表**直接成为 `ToolMessage.content`**。

`_read_image` 应复用同一机制：

```python
def _read_image(file_path: str, session_id: str, ...) -> str | list[dict]:
    raw = Path(file_path).read_bytes()
    max_raw = READ_IMAGE_MAX_BYTES  # 20 MB, aligns with ZCode Tde
    if len(raw) > max_raw:
        return json.dumps({"error": "read_image_input_too_large", "max_bytes": max_raw})
    # 1. 压缩（压缩级联计划）
    compressed = prepare_for_model(raw, ...)
    # 2. 写到磁盘（与截图同一持久化路径，供 native_image_content 读取）
    saved_path = _save_compressed_image(session_id, compressed)
    # 3. 文本描述（保留路径供 skill 回退路径使用）
    text = f"[Image read: {file_path} ({len(raw)} bytes, {compressed.dimensions[0]}x{compressed.dimensions[1]})]"
    # 4. 复用 native_image_content 构造 ToolMessage 内容
    from agent.middlewares.media_pipeline.tool_media import native_image_content
    return native_image_content(session_id, text, saved_path)
```

`native_image_content` 内部流程：
1. 检查 `_native_allowed(session_id)` —— 模型不支持图片 → 只返回 text（skill 回退路径）
2. `_image_block(saved_path)` —— 读文件 → base64 → `{"type": "image_url", "image_url": {"url": "data:..."}}`
3. 返回 `[{"type": "text", "text": text}, image_block]` —— 直接成为 `ToolMessage.content`

**这样图片块通过 ToolMessage 的多模态 content 到达模型，与浏览器截图完全相同的路径。**

### 2.3 能力门控

在返回图片块前，检查主模型是否支持图片：
- 查询 vision 能力系统（见计划 #8 `Vision 能力门控`）
- 若 `supportsImages == False`：回退至现有 `image_to_text` skill 路径（不改变非 vision 模型的当前行为）
- 若 `supportsImages == True` 或 `"auto"`：返回图片块

### 2.4 不变内容

- `image_to_text` skill 保留为非 vision 模型的 fallback
- `_BINARY_EXTENSIONS` 无需新增图片扩展名（图片在该检查前走不同分支）
- 读取状态许可（`note_read` / `licensed`）不适用于图片读取（图片无读后写契约）

## 3. 变更清单

| 文件 | 变更 |
|---|---|
| `agent/tools/file_tools/read_file.py` | 新增 `IMAGE_EXTENSIONS` 集合；新增 `_read_image()` 函数（调用 `prepare_for_model` + `native_image_content`）；在 `_core` 文本处理前新增分支 |
| `agent/middlewares/media_pipeline/tool_media.py` | `native_image_content` 已支持图片路径读取，无需改动；`_image_block` 会被 `_read_image` 间接复用 |
| `config/features/agent_side/tools_timeouts.py` | 新增 `read_image_max_bytes: int = 20 * 1024 * 1024` |
| `agent/tools/file_tools/read_file.py` args_schema | 不改 `ReadFileInput` —— `file_path` 已接受任意路径；分支为内部逻辑 |

## 4. 测试

| 文件 | 测试 |
|---|---|
| `tests/agent/tools/file_tools/test_read_file.py` | 图片文件 → 返回 `image_url` 块；超大图片 → `read_image_input_too_large`；非 vision 模型 → 回退至 skill 提示 |

## 5. 备注

- 这是与 ZCode 最大的架构分歧。Sherry 的 `image_to_text` skill 路径对非 vision 模型是有效替代 —— 本计划将其保留为 fallback 而非移除。
- 压缩级联（`prepare_for_model`）和魔法数检测（`_detect_image_mime`）与计划 #3 和 #4 共享。
