# 计划：Vision 能力门控（对齐 ZCode）

- 日期：2026-11-10
- 状态：**待评审**
- 来源：ZCode 对齐 — ZCode 在模型注册表中为每个模型显式声明 `supportsImages` / `supportsPdf` / `supportsVideo` 能力；Sherry 仅使用运行时反应式探测
- 风险等级：**中等** — 改变媒体按模型路由的方式；涉及能力缓存、模型 config 和媒体流水线
- 依赖：无（Read 工具图片路径和 PDF 转换的基础）

---

## 0. ZCode 行为

### 0.1 声明式模型目录 — JSON 文件，非硬编码

ZCode 的模型能力来自一个**独立的 JSON 目录文件**：`resources/model-providers/models_catalog_china_llm_zcode_2026-06-03.json`（schema 版本 `zcode.model-providers.v1`）。该文件列出每个受支持的 provider 和模型及其能力、上下文窗口和定价 — 全部声明式，无运行时学习。

bundle 中的模型注册表读取该目录并暴露每个模型的标志：
```js
{
  contextWindow: t.contextWindow ?? 0,
  id: e.modelId,
  name: e.modelId,
  supportsImages: t.supportsImages ?? false,
  supportsReasoning: r,
  supportsStructuredOutput: ...
}
```

运行时检查：
- `if(n.type==="image" && e.supportsImages===!1) throw "Model does not support image input"`
- `if(iK(e) && t?.supportsPdf===!1) return "PDF input"`（返回一个友好的原因字符串）

**ZCode 从不在运行时探测。** 目录声明每个模型支持什么，运行时据此门控媒体。不在目录中的模型被视为不支持媒体 — 没有"试错学习"路径。

ZCode 是**智谱AI**产品（通过 `config/default.json` → `zhipu-ai.feishu.cn` 确认），GLM 模型系列是其自有产品。目录文件名包含日期戳（`2026-06-03`），表明它被版本化管理并与应用 bundle 独立更新。

## 1. Sherry 当前状态（已验证）

### 1.1 运行时能力缓存已存在 — `llm_capability_cache.py`

**`agent/middlewares/llm_capability_cache.py`** — 进程级内存字典 `_cache`，以 `"{provider}/{model}"` 为键，每种媒体类型取值为 `"auto"`/`"supported"`/`"unsupported"`，覆盖 `vision`/`audio`/`video`。这正是 JSON 目录要填充的运行时：
- `get_capability(provider, model, media)` → 返回缓存值（由 `_should_keep_native`、`scrub_messages`、`_scrub_request` 消费）
- `set_capability(provider, model, media, value)` → 写入缓存（目前仅以 `"unsupported"` 调用）

目录会在启动时**播种**该缓存 — 替换已知模型的 `"auto"` 冷启动默认值。

### 1.2 持久化模式已存在 — `model_caps.py`

**`agent/tools/subagent/model_caps.py`** — 一个按 `(provider, model)` 持久化的 JSON 文件，用于结构化输出降级等级（0/1/2）：
- `subagent_model_caps_path()` → `src/data/subagent_model_caps.json`
- `_save_locked()` → 通过 `atomic_write_text_no_follow` 原子写入 JSON
- 首次访问时惰性加载，解析错误时 fail-open
- 这正是要复制用于能力目录文件的持久化模式。

### 1.3 config 表先例已存在 — `MODEL_PRICING_TABLE`

**`config/models_settings.py:147-152`** — `MODEL_PRICING_TABLE` 是一个按模型的元数据表，序列化为 `.models` config 文件中的 JSON 字符串，以模型名为键，含 `{input, output}` USD 定价。这证明 `.models` config 已支持按模型的元数据表 — 一个 `MODEL_CAPABILITIES_TABLE` 可复用相同的 `_JSON_DICT_KEYS` + `_coerce` 机制。

### 1.4 会读取目录的消费者（无需改动 — 它们已调用 `get_capability`）

| 消费者 | 文件 | 作用 |
|---|---|---|
| `_should_keep_native` | `agent/middlewares/media_pipeline/core.py:139-163` | 决定原生路径 vs skill 路径 |
| `_scrub_request` | `agent/middlewares/media_pipeline/core.py:168-185` | 按请求剥离不支持的媒体 |
| `scrub_messages` / `_scrub_content` | `agent/middlewares/media_pipeline/scrub.py:27-83` | 用文本占位符替换不支持的块 |
| `_evaluate_silent_degradation` | `agent/middlewares/llm_retry/core.py:750-790` | 检测到盲区时缓存 `"unsupported"` |

## 2. 设计

### 2.1 声明式能力目录 — JSON 文件，对齐 ZCode

ZCode 的 `supportsImages` 来自独立的 JSON 目录文件（`models_catalog_china_llm_zcode_2026-06-03.json`，schema `zcode.model-providers.v1`）。Sherry 应镜像该做法：使用**JSON 目录文件**而非硬编码的 Python 字典，这样可在不改动代码的情况下更新。

新文件：

**`config/model_capabilities.py`** — 加载目录并提供查询：
```python
class ModelCapabilities(TypedDict, total=False):
    supports_images: bool
    supports_pdf: bool
    supports_video: bool

def _load_catalog() -> dict[str, ModelCapabilities]:
    """Load the JSON model catalog (aligns with ZCode's models_catalog_*.json)."""
    path = MODEL_CAPABILITIES_CATALOG_PATH  # src/data/model_capabilities.json
    if path.exists():
        return json.loads(path.read_text())
    return {}

def get_declared_capability(provider: str, model: str, media: str) -> str | None:
    """Returns 'supported' / 'unsupported' / None (not declared)."""
    catalog = _load_catalog()
    caps = catalog.get(f"{provider}/{model}")
    if not caps:
        return None
    field = {"vision": "supports_images", "pdf": "supports_pdf", "video": "supports_video"}[media]
    if field in caps:
        return "supported" if caps[field] else "unsupported"
    return None
```

**`src/data/model_capabilities.json`** — 目录文件（数据，非代码）：
```json
{
  "openai/gpt-4o": {"supports_images": true, "supports_pdf": true},
  "openai/gpt-4.1": {"supports_images": true},
  "anthropic/claude-sonnet-4-5": {"supports_images": true, "supports_pdf": true},
  "google/gemini-2.5-flash": {"supports_images": true, "supports_pdf": true, "supports_video": true},
  "deepseek/deepseek-v3": {"supports_images": false}
}
```

该目录：
- **版本化**，文件名带日期戳（对齐 ZCode 的 `models_catalog_china_llm_zcode_2026-06-03.json`）
- **可更新**，无需代码发布（只需发布新的 JSON 文件）
- **可覆盖**，通过 `sherry.jsonc` 的 `model_capabilities` 键（用户/企业覆盖）

### 2.2 接入能力缓存

修改 `llm_capability_cache.get_capability()`，首先查询声明式注册表：

```python
def get_capability(provider: str, model: str, media: str) -> str:
    model_key = f"{provider}/{model}"
    # 1. Check declarative registry first
    declared = MODEL_CAPABILITY_TABLE.get(model_key, {})
    field = {  # media type → registry field
        "vision": "supports_images",
        "audio": "supports_audio",   # future
        "video": "supports_video",
        "pdf": "supports_pdf",        # new media type
    }.get(media)
    if field and field in declared:
        return "supported" if declared[field] else "unsupported"
    # 2. Fall back to runtime cache (reactive probing)
    return _cache.get(model_key, {}).get(media, "auto")
```

### 2.3 新增 `"pdf"` 媒体类型

媒体流水线目前没有 PDF 概念：
- 在 `media_handlers.py` 的 `MediaType` Literal 中新增 `"pdf"`
- 在 `MEDIA_TYPE_BY_ITEM` 映射中新增 PDF
- 新增 PDF handler（或在渲染后通过图片 handler 路由 PDF — 见计划 #2）
- `scrub.py` 的 `_FAMILY_SKILL` 新增 `"pdf": None`（PDF 无 skill 回退；若模型无法处理则拒绝）

### 2.4 持久化运行时缓存

镜像 `model_caps.py`：
- 将学习到的能力持久化到 `src/data/model_capabilities.json`
- 启动扫描将持久化缓存加载到 `_cache`
- 跨重启保留 — 上次会话标记为 `"unsupported"` 的模型保持标记

### 2.5 成功时写入 `"supported"` — 替代运行时学习

**当前 Sherry 行为：缓存只会将某媒体家族从 `"auto"` 转为 `"unsupported"`，从不写入 `"supported"`** — `set_capability` 仅以 `"unsupported"` 调用（模型拒绝或静默降级时）。成功处理媒体的模型永远停留在 `"auto"`，意味着原生探测每轮都会重复。

**ZCode 行为：完全没有运行时学习。** JSON 目录预声明每个模型的能力。没有"试错学习"路径 — 不在目录中的模型被视为不支持媒体。

对齐方案：
1. **主路径**：JSON 目录预声明 `"supported"` / `"unsupported"` — 已目录化的模型无需探测（这是 ZCode 路径）
2. **回退**：对于不在目录中的模型，保留运行时探测 — 但在成功的原生调用后也写入 `"supported"`，使探测不会每轮重复。这相比当前 Sherry（从不写入 `"supported"`）和 ZCode（对未知模型无回退）都是改进。

```python
# After a successful native multimodal call with no rejection:
for media in present_media_types:
    if get_capability(provider, model, media) == "auto":  # still unprobed
        set_capability(provider, model, media, "supported")  # learn positive result
```

这意味着：
- 已目录化的模型 → 声明式答案，零探测（对齐 ZCode）
- 成功的未目录化模型 → 首轮后 `"supported"`，不再重复探测（相比当前 Sherry 是改进）
- 失败的未目录化模型 → 首次失败后 `"unsupported"`（与当前 Sherry 不变）

### 2.6 通过 `sherry.jsonc` 用户覆盖

允许按模型覆盖：
```jsonc
{
  "model_capabilities": {
    "custom-provider/my-model": {"supports_images": true}
  }
}
```
这覆盖静态表和运行时缓存。

## 3. 变更清单

| 文件 | 变更 |
|---|---|
| `config/model_capabilities.py` | **新文件** — `ModelCapabilities` TypedDict、`get_declared_capability()`、`_load_catalog()` |
| `src/data/model_capabilities.json` | **新数据文件** — JSON 模型目录（对齐 ZCode 的 `models_catalog_*.json`） |
| `agent/middlewares/llm_capability_cache.py` | `get_capability()` 先查询 JSON 目录；新增 `"pdf"` 媒体类型；持久化到 `src/data/model_capabilities_runtime.json`；成功时写入 `"supported"` |
| `agent/middlewares/media_pipeline/media_handlers.py` | 在 `MediaType` 和 `MEDIA_TYPE_BY_ITEM` 中新增 `"pdf"` |
| `agent/middlewares/media_pipeline/scrub.py` | 在 `_FAMILY_SKILL` 中新增 `"pdf"`（无 skill 回退） |
| `config/sherry_settings.py` | 新增 `model_capabilities` 用户覆盖键（合并覆盖 JSON 目录） |
| `config/features/__init__.py` | 导出 `get_declared_capability` |

## 4. 测试

| 文件 | 测试 |
|---|---|
| `tests/agent/middlewares/test_llm_capability_cache.py` | 已声明模型 → 不探测返回声明值；未声明模型 → 回退到运行时缓存；启动时加载持久化缓存；成功时写入 `"supported"` |
| `tests/config/test_model_capabilities.py | 按 `(provider, model)` 查询注册表；未知模型 → `None`；通过 `sherry.jsonc` 的用户覆盖优先 |

## 5. 备注

- JSON 目录（`src/data/model_capabilities.json`）应从 ZCode 的目录数据（`models_catalog_china_llm_zcode_2026-06-03.json`，schema `zcode.model-providers.v1`）填充。这是一次性数据导入 — `supportsImages`/`supportsPdf`/`supportsVideo` 字段可直接提取。
- 目录是**数据文件**而非代码 — 可独立于代码发布进行更新和发布，与 ZCode 的版本化目录文件完全一致。
- 反应式探测路径作为不在目录中的模型的回退被保留 — 这实际上比 ZCode 的纯声明式方式更健壮（ZCode 将未知模型视为不支持媒体；Sherry 探测一次并学习）。
- 相对当前 Sherry 的关键行为变化：**已目录化的模型完全跳过探测**（声明式答案），**成功的探测写入 `"supported"`**（使未目录化模型不会每轮重复探测）。当前 Sherry 只写入 `"unsupported"` 并永远重复探测。
- 本计划是基础：计划 #1（Read 读取图片）和 #2（PDF 转换）都需要在发送媒体前检查 `supports_images` / `supports_pdf`。
