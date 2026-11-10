# 计划：图片压缩级联（对齐 ZCode）

- 日期：2026-11-10（更新于 2026-11-10：移除 `estimate_tokens.py` 改动 —— ZCode 不使用本地图片 token 估算做 context 预算）
- 状态：**待评审**
- 来源：ZCode 对齐 —— 复刻 ZCode 的 Jimp 风格压缩级联及其 base64 长度 token 门；无任何创新
- 风险等级：**低-中** —— 在发送路径上新增一个图片处理步骤；不改变 context-window token 估算或压缩/驱逐时机
- 耦合说明：token 门（`ceil(base64Len / 8) <= 25000`）内建在压缩级联中作为其终止条件。它不是独立特性，也不馈入 context-window token 估算器。

---

## 0. ZCode 的行为（逆向工程，已验证）

来源：`D:\IDE\ZCode\resources\glm\zcode.cjs`（GLM 打包文件，约 12.6 MB minified）。

### 0.1 图片 token 估算 —— 仅作压缩门，非 context 预算

ZCode 的图片 token 公式：

```
base64CharCount = ceil(rawByteLength / 3) * 4
imageTokens = ceil(base64CharCount * 0.125)
           = ceil(base64CharCount / 8)
```

关键函数：`Elo(n)` = `Math.ceil(n/3)*4`，`SQ(buffer, budget)` = 门。

**关键发现：此公式仅作为 `prepareForModel` 内部的压缩决策门使用。** 它决定图片在发送前是否必须缩小。它不用于 context-window token 预算。

ZCode 的 context-window 管理使用：
- **provider 上报的 `usage.input_tokens`**（主信号 —— 从 API 响应解析的 `promptTokens`）
- **`estimateTokens` 做纯文本估算**（CJK 感知：`(cjk*2 + non_cjk) / 3` —— 函数 `is` 位于偏移 7681125）。此函数完全不处理图片内容块。

ZCode 中没有 per-provider 图片 token 公式（无 OpenAI tile 公式 512/170/85，无 Anthropic /750）。`0.125` 比率仅出现在图片压缩模块中（`SQ` / `createImageBudget` / `prepareForModel`）。

### 0.2 图片压缩级联

仅后端（不在 Electron renderer 中）。所有图片经由基于 Jimp 的处理器（`JimpImageProcessorAdapter` / `DZe`），方法 `prepareForModel` → `Gtr`。

**预算常量：**

| 常量 | 值 | 含义 |
|---|---|---|
| `G1` | 5 MB | maxBase64Bytes（user/reference/PDF 分支） |
| `iC` | 3.75 MB | maxRawBytes = floor(G1 × 3/4) |
| `i4` | 2000 px | maxDimension（最长边） |
| `hg` | 25000 | maxTokens 目标（门） |
| `Cde` | 0.125 | tokenToBase64CharRatio |
| `GI` | 200 KB | maxBase64Bytes（MCP oversized-image 分支） |

**级联算法**（`Olo`，每个候选必须通过 `SQ` 字节+token 预算）：

1. **小图直通** —— 尺寸 ≤ 2000px 且 `SQ(rawBytes)` 通过 → 发送原图（`strategy:"original"`）
2. **格式保持重编码** —— PNG：`deflateLevel:9, deflateStrategy:3`；JPEG：quality 80→60→40→20；GIF：保持
3. **缩放至 2000px**（BICUBIC `scaleToFit`）→ 重试格式保持 + JPEG quality 梯度
4. **降采样梯度** —— 最长边的 75% → 50% → 25%，每次重试格式保持 + JPEG quality
5. **硬兜底** —— 缩放至 1000→800→600→400→300→200px，强制 JPEG quality 20
6. 全部失败 → 抛出 `too_large`

**格式规则：** PNG 默认不转换为 JPEG（仅在 PNG 持续超预算时作为后续 fallback）。WebP 达标时透传（压缩率已高，重编码无意义），不达标时走 PIL WebP 压缩级联（resize + quality 80→60→40→20），最后兜底转 JPEG q20 —— ZCode 因 Jimp 不支持 WebP 只能透传或报错，Sherry 用 PIL 可以实际压缩。

---

## 1. Sherry 当前状态（已验证）

### 1.1 Context-window token 估算 —— 已与 ZCode 对齐，勿改

**`pub/func/estimate_tokens.py`** —— `_estimate_block_tokens`（201-215 行）：对每个 `image_url` 块返回固定 85 token。这是用于预防性摘要决策的本地启发式。

**Sherry 的 `ContextLimitGuard`（`agent/wrapper/context_limit.py`）已使用 provider 上报的 `usage_metadata.input_tokens`** —— 而非本地估算器 —— 做 context 硬上限检查。这与 ZCode 的做法一致。

本地估算器中对图片块的固定 85 是一个保守占位值。它不需要改为 `base64Len / 8`，因为：
- ZCode 也不使用本地图片 token 公式做 context 预算
- ZCode 的 `estimateTokens` 是纯文本的，正如 Sherry 的 `estimate_text_tokens`
- 两套系统都依赖 provider 上报 token 做实际 context-window 追踪
- 将本地估算器改为 `base64Len / 8` 将是创新而非对齐

### 1.2 无图片压缩 —— 图片原样发送

不存在活跃压缩。死代码函数 `compress_image_if_needed`（`pub/func/media/image.py`，73-137 行）零活跃调用方。

所有图片 —— 用户上传、白板 PNG、浏览器截图、skill-path —— 以原始分辨率和字节大小到达 LLM，仅受硬字节上限约束（`max_media_bytes` = 20 MiB，`tool_media_attach_max_bytes` = 2 MiB）。

### 1.3 五个图片入口（须全覆盖）

| # | 入口 | 文件 | 承载内容 |
|---|---|---|---|
| 1 | WS/Tauri 用户发送 | `server/service/messages.py::_get_content_list`（52-68 行） | 来自用户粘贴/选择器/白板的 `image_base64_list` + `image_bytes_list` |
| 2 | Media middleware | `agent/middlewares/media_pipeline/media_handlers.py::ImageUrlHandler.process`（294-328 行） | `data:` base64 解码为 PIL `Image`，原样重存 |
| 3 | 浏览器截图 | `agent/middlewares/media_pipeline/tool_media.py::_image_block`（93-107 行） | PNG 从磁盘 → base64 放入 `ToolMessage` |
| 4 | HTTP 上传（浏览器模式） | `server/trigger/http/image.py::upload_image`（23-73 行） | 原始字节 → 文件 → URL；不压缩 |
| 5 | Image-to-text skill | `skills/builtin/core/image_to_text/scripts/core.py`（89-115 行） | 原始文件 → base64 → vision model |

---

## 2. 设计

### 2.1 带内建 token 门的压缩级联

**新模块：** `pub/func/media/image_compress.py`

token 门（`fits_image_budget`）是此模块内的一个函数 —— 不导出，也不在其他任何地方使用：

```python
def _fits_image_budget(raw_bytes: bytes, budget: ImageBudget) -> bool:
    """ZCode's SQ gate: ceil(base64CharCount * 0.125) <= max_tokens."""
    base64_len = _base64_encoded_length(len(raw_bytes))  # ceil(n/3)*4
    token_estimate = math.ceil(base64_len * budget.token_ratio)  # *0.125
    return (
        len(raw_bytes) <= budget.max_raw_bytes
        and base64_len <= budget.max_base64_bytes
        and token_estimate <= budget.max_tokens
    )
```

级联（`prepare_for_model`）使用此门作为终止条件 —— 每个候选仅在 `_fits_image_budget(candidate_bytes, budget)` 通过时才被接受。

### 2.2 级联实现（PIL）

```python
def prepare_for_model(
    raw: bytes,
    *,
    max_dimension: int = 2000,
    max_tokens: int = 25000,
    token_ratio: float = 0.125,
    max_base64_bytes: int = 5 * 1024 * 1024,
) -> CompressedImage:
    budget = ImageBudget(max_base64_bytes, max_raw_bytes=max_base64_bytes * 3 // 4,
                         max_tokens=max_tokens, token_ratio=token_ratio)
    fmt, mime, ext = detect_image_format(raw)  # 复用 media_handlers.py::_infer_extension（已存在，无需新建）

    # WebP: 达标透传，不达标走 PIL 压缩（ZCode 因 Jimp 不支持只能透传或报错，
    # Sherry 用 PIL 能 resize + 重编码 WebP）
    if fmt == ImageFormat.WEBP:
        if _fits_image_budget(raw, budget):
            return CompressedImage(data=raw, media_type="image/webp",
                                   strategy="webp-passthrough", ...)
        # 不达标 → resize + WebP quality 80→60→40→20 → 兜底 JPEG q20
        return _compress_webp(raw, budget, max_dimension)

    # Small image passthrough
    if _fits_image_budget(raw, budget):
        img = Image.open(io.BytesIO(raw))
        if max(img.size) <= max_dimension:
            return CompressedImage(data=raw, strategy="original", ...)

    # Cascade: format-preserving → resize → quality ladder → downscale → hard fallback
    return _find_first_fitting_candidate(raw, fmt, max_dimension, budget)
```

候选梯度（镜像 ZCode 的 `Olo`）：
1. 格式保持重编码：PNG `optimize=True`；JPEG quality 80→60→40→20；GIF 保持
2. 缩放至 `max_dimension`（LANCZOS）→ 重试格式保持 + JPEG quality
3. 降采样 75%→50%→25% → 重试
4. 硬兜底：1000→800→600→400→300→200px，JPEG quality 20
5. 全部失败 → 抛出 `ImageTooLargeError`

### 2.3 不变内容（对齐确认）

| 组件 | ZCode | Sherry | 动作 |
|---|---|---|---|
| Context-window token 估算 | `estimateTokens` = 纯文本（CJK 感知） | `estimate_text_tokens` = 纯文本（CJK 感知） | **不变** —— 已对齐 |
| 本地估算器中的图片块 | 不计入（纯文本函数） | 固定 85 token | **不变** —— 均为本地启发式，不影响 context 硬上限 |
| Context 硬上限 | provider 上报 `input_tokens` | `ContextLimitGuard` 使用 `usage_metadata.input_tokens` | **不变** —— 已对齐 |
| `estimate_tokens.py::_estimate_block_tokens` | 不适用（ZCode 本地估算器无图片 token） | 图片块固定 85 | **不变** |

### 2.4 插入点

| # | 文件 | 变更 |
|---|---|---|
| 1 | `server/service/messages.py::_get_content_list`（52-68 行） | 在包装为 `data:` URL 前通过 `prepare_for_model` 压缩 `image_base64_list` / `image_bytes_list` |
| 2 | `agent/middlewares/media_pipeline/media_handlers.py::ImageUrlHandler.process`（294-328 行） | 在 `image.save` 前压缩解码后的 `image_bytes` —— 或依赖 #1 并跳过（若 #1 先运行则冗余） |
| 3 | `agent/middlewares/media_pipeline/tool_media.py::_image_block`（93-107 行） | 在 base64 前通过 `prepare_for_model` 压缩截图 PNG |
| 4 | `server/trigger/http/image.py::upload_image`（23-73 行） | 在写入磁盘前压缩上传字节 |
| 5 | `skills/builtin/core/image_to_text/scripts/core.py`（89-115 行） | 在发送至 vision model 前压缩 —— 低优先级 |

建议：在 #1 + #3 + #4 压缩（覆盖主路径），跳过 #2（冗余）和 #5（低流量 fallback）。

### 2.5 配置

| 文件 | 变更 |
|---|---|
| `config/features/agent_side/media_pipeline.py` | 新增 `image_compress_max_dimension: int = 2000`、`image_compress_max_tokens: int = 25000`、`image_compress_token_ratio: float = 0.125`、`image_compress_enabled: bool = True` |

### 2.6 死代码

删除 `pub/func/media/image.py`（73-137 行）中的 `compress_image_if_needed` —— 被新级联取代。更新 `pub/func/media/__init__.py` 中的重导出。

---

## 3. 变更清单

### 3.1 压缩模块

| 文件 | 变更 |
|---|---|
| `pub/func/media/image_compress.py` | **新文件** —— `prepare_for_model()` 级联 + `_fits_image_budget()` 门，从 ZCode 的 Jimp 逻辑移植到 PIL |
| `pub/func/media/image.py` | 删除死代码 `compress_image_if_needed`；更新重导出 |
| `config/features/agent_side/media_pipeline.py` | 新增压缩配置键 |

### 3.2 将压缩接入入口

| 文件 | 变更 |
|---|---|
| `server/service/messages.py` | `_get_content_list`：通过 `prepare_for_model` 压缩 `image_base64_list` / `image_bytes_list` |
| `agent/middlewares/media_pipeline/tool_media.py` | `_image_block`：通过 `prepare_for_model` 压缩截图 PNG |
| `server/trigger/http/image.py` | `upload_image`：在写入前压缩上传字节 |

### 3.3 不变

| 文件 | 原因 |
|---|---|
| `pub/func/estimate_tokens.py` | ZCode 不使用本地图片 token 估算做 context 预算；Sherry 的固定 85 + provider 上报 token 已对齐 |
| `config/features/agent_side/token_estimation.py` | 无需新增 token 估算配置 |
| `agent/wrapper/context_limit.py` | 已使用 provider 上报 token，与 ZCode 对齐 |

### 3.4 测试

| 文件 | 测试 |
|---|---|
| `tests/pub/func/media/test_image_compress.py` | **新增** —— 小图直通；大 PNG → JPEG fallback；quality 梯度；尺寸上限 2000px；token 门（`base64Len/8 <= 25000`）；WebP 达标透传 + 不达标走 PIL 压缩 + JPEG 兜底；不可压缩时 `too_large` |
| `tests/server/service/test_messages.py` | `_get_content_list` 在构造 `data:` URL 前压缩 base64/bytes 图片 |
| `tests/agent/middlewares/media_pipeline/test_tool_media.py` | 截图 PNG 在 base64 嵌入前压缩 |

### 3.5 文档

- AGENTS.md：在 media pipeline 下新增 "图片压缩级联" 章节；注明 token 估算仅作压缩门（非 context 预算）

---

## 4. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 发送热路径上的压缩延迟（级联尝试多个候选） | 快路径：若 `_fits_image_budget(raw)` 通过且尺寸 ≤ 2000px，完全跳过级联（直通）；测量 p50/p95 |
| PIL `optimize=True` ≠ ZCode `deflateLevel:9, deflateStrategy:3` | 可接受 —— PIL 的 `optimize=True` 是最接近的等价；不要求字节级一致输出，仅需 "符合预算" |
| WebP 图片需要压缩时 | PIL 能读写 WebP（ZCode 的 Jimp 不能）：达标透传，不达标走 PIL resize + WebP quality 梯度，兜底转 JPEG q20 |
| PNG→JPEG 丢失 alpha 透明度 | 遵循 ZCode 级联顺序：先 PNG 重编码；仅在 PNG 持续超预算时作为后续 fallback 使用 JPEG |

### 4.1 Token 估算风险：per-image 门非累积（继承自 ZCode）

**此风险在 ZCode 中同样存在 —— 它是对齐方案的固有属性，非 Sherry 特有 bug。**

`base64/8 ≤ 25000` 门按 **per-image** 检查，而非跨会话累积。一个含 10 张压缩图片（每张 ≤25000 门 token）的会话可能累计携带数以千计的实际 provider token：

| Provider | 每张压缩图片的实际 token（2000×2000 JPEG） | 10 张图片累计 |
|---|---|---|
| OpenAI（tile 公式） | ~2805 | ~28050 |
| Anthropic（/750 公式） | ~5333 | ~53330 |

对于 32K context 的模型，28050 图片 token + 会话文本可能溢出。门不会捕获此情况，因为它仅在摄取时按 per-image 运行。

**为何仍足够安全（与 ZCode 一致）：**
- `base64/8` 相对所有已知 provider 公式都高估 → 压缩比所需更激进 → 单张图片比门允许的更小
- `ContextLimitGuard`（Sherry）/ `shouldAutoCompact`（ZCode）在每次调用后使用 **provider 上报的** `input_tokens` → 反应式捕获累积溢出
- history 剥离预算（75MB media bucket，计划 #11）对 context 中总 media 字节提供第二启发式上限
- 溢出时：guard 触发摘要/压缩 → media 被卸载/剥离 → 重试成功

**残余代价：** 首次溢出时浪费一次 API 调用（触发反应式 guard 的那次调用）。此代价与 ZCode 相同。

### 4.2 跨 provider 差异：base64/8 不匹配任何 provider 的公式

base64/8 是通用代理，非任何 provider 的实际图片 token 公式。这意味着：

| 效果 | 方向 | 风险 |
|---|---|---|
| per-image 压缩激进度 | base64/8 > 实际 → 压缩比所需更多 | fail-safe（过度压缩，非压缩不足） |
| 累积 context 预算 | 无本地估算 → 无预防性累积检查 | 反应式 guard 捕获（与 ZCode 一致） |
| provider 专属优化 | 不可能（例如 OpenAI 的 low-detail 模式 = 固定 85 token） | 未对齐；ZCode 也不做 |

**无需行动** —— 这是对齐行为。ZCode 有相同差异和相同反应式兜底。

### 4.3 音频/视频无预防式 token 检查（继承自 ZCode）

ZCode 和（对齐后）Sherry 只对**图片**做 token 估算（`base64/8` 压缩门）。音频和视频是透传：原始 base64、无压缩、无 token 估算。实际 token 成本在 provider API 调用返回前未知。

| 媒体 | 大小限制 | 实际 token 成本示例 | 预防式检查？ |
|---|---|---|---|
| 音频 (wav) | 20MB 通用附件上限 | OpenAI gpt-4o-audio: ~150 token/s → 20MB wav (~600s) ≈ 90000 token | **无** |
| 视频 (mp4) | 30MB 硬限（见历史剥离计划） | Google Gemini: ~263 token/s → 30MB 1080p (~30s) ≈ 8000 token | **无** |
| 图片（压缩后） | 25000 门 token (≤200KB) | OpenAI tile: ~2800; Anthropic /750: ~5300 | 逐图（非累计） |

溢出时的流程：消息发送 → provider 报错或高 `input_tokens` → `ContextLimitGuard`（Sherry）/ `shouldAutoCompact`（ZCode）检测溢出 → 触发压缩/卸载 → 重试。代价：浪费一次 API 调用。

**这是对齐行为** —— ZCode 有相同风险和相同反应式兜底。添加 per-provider 音视频 token 公式做预防式检查是创新，不在对齐范围内。

### 4.4 视频 30MB 硬限

对齐 ZCode `Ede = 31457280`（30MB）。在 `media_handlers.py` 和 `tool_media.py` 的视频 block 附加前检查 `len(raw) > 30MB` → 拒绝。此限制是模型侧的（不是传输侧的 500MB 上传上限）。视频历史剥离预算桶（30MB）在历史剥离计划中。视频不做压缩（ZCode 也不做）。`supportsVideo` 能力声明在 Vision 门控计划中。

---

## 5. 实现顺序

1. 配置 —— 向 `media_pipeline.py` 添加压缩键
2. 新模块 —— `pub/func/media/image_compress.py`（带 token 门的 PIL 级联）
3. 接入压缩 —— `messages.py`（#1）+ `tool_media.py`（#3）+ `http/image.py`（#4）
4. 测试 —— 压缩器 + 集成
5. 删除死代码 —— `compress_image_if_needed`
6. 文档 —— AGENTS.md
