# Multimodal Processor: Native-First + Skill Fallback

> **Status**: Plan
> **Date**: 2026-09-14
> **Goal**: 当主 LLM 原生支持多模态时直接传 image_url block，不支持时 fallback 到技能路径；检测结果按模型缓存于进程内存。

## 1. 现状分析

### 1.1 当前流程（无条件走技能）

```
User 上传图片 → MultimodalProcessor.before_agent
  → handler.process() 存盘 + 记录 paths
  → last_mes.content = [text_dict]          ← 剥离 image_url block
  → _attach_media_hints()                  ← 硬编码 "model has no native vision"
  → 模型收到纯文本提示 → 调 skill_view('image_to_text') → terminal 工具
```

**问题**：硬编码假设模型无 vision 能力（`agent/middlewares/media_pipeline/core.py:119` `"current model has no native vision ability"`）。如果主 LLM 是 GPT-4o / Claude / Gemini 等原生支持多模态的模型，图片被白白剥离，模型被强制走技能绕路。

### 1.2 架构约束

| 组件                                   | 职责                   | 限制                              |
| -------------------------------------- | ---------------------- | --------------------------------- |
| `MultimodalProcessor` (before_agent)   | 处理上传媒体           | 早于模型调用，不能 catch 模型错误 |
| `LLMRetryMiddleware` (wrap_model_call) | 包裹模型调用，错误重试 | 可拦截模型错误并修改 messages     |
| `state_register_mem`                   | session 级内存状态     | 不跨 session                      |
| 模块级 dict (新增)                     | 进程级内存缓存         | 跨 session 共享，不跨重启         |

### 1.3 涉及文件一览

| 文件                                                | 行数 | 角色                                       |
| --------------------------------------------------- | ---- | ------------------------------------------ |
| `agent/middlewares/media_pipeline/core.py`               | 232  | 媒体处理 + 技能提示注入                    |
| `agent/middlewares/media_pipeline/media_handlers.py`               | 301  | 各媒体类型处理器（存盘）                   |
| `agent/middlewares/llm_retry/core.py`                    | 425  | LLM 调用重试 + fallback chain              |
| `pub/func/message/llm_error_classifier.py`          | 470  | 错误分类引擎                               |
| `config/features/agent_side/media_pipeline.py`      | 14   | MEDIA_PIPELINE 配置（唯一 TypedDict）      |
| `models/LLMs/main_llm.py`                           | 169  | 主 LLM 构建（env: MAIN_LLM_PROVIDER/NAME） |

## 2. 设计方案

### 2.1 核心思路

```
before_agent (MultimodalProcessor)
  │
  ├─ 用户配置 "true"   → 保留 block，不试
  ├─ 用户配置 "false"  → 剥离 + 技能提示（当前行为）
  ├─ 用户配置 "auto":
  │    ├─ 缓存[当前模型][media] = "supported"    → 保留 block（已验证支持）
  │    ├─ 缓存[当前模型][media] = "unsupported"  → 剥离 + 技能提示（已验证不支持）
  │    └─ 缓存[当前模型][media] = "auto"/missing → 保留 block + 设 session flag
  │
  └─ 各媒体类型独立判断
       (vision 可能 supported 而 audio unsupported)

wrap_model_call (LLMRetry)
  │
  └─ 模型报 multimodal_not_supported 错误
       ├─ 检查 session flag _trying_native
       ├─ 写内存缓存: 当前模型 + 对应媒体类型 = "unsupported"
       ├─ 调用 apply_skill_fallback() 修改 messages（剥离 + 加技能提示）
       └─ retry → 成功则完成
```

### 2.2 模型切换场景

用户切换不同模型时（修改 `MAIN_LLM_PROVIDER` / `MAIN_LLM_NAME` 环境变量后重启），缓存必须按模型身份隔离，避免用旧模型的检测结果。

**缓存 key**: `"{provider}/{model_name}"`

```
场景 1: GPT-4o → deepseek-chat
  GPT-4o: 缓存 vision="supported"
  切换后: 服务器重启 → 进程级内存清空 → deepseek-chat 缓存缺失 → "auto" → 尝试原生 → 失败 → 写 "unsupported"

场景 2: 同一进程内 fallback chain 模型
  主模型 gpt-4o (vision=supported) 故障 → LLMRetry 切到 fallback deepseek-chat
  deepseek-chat 不支持 vision → 报 multimodal_not_supported
  → 写 deepseek-chat 缓存 = "unsupported" → fallback 到技能路径
  → 同进程后续切到 deepseek-chat 直接走技能，不浪费调用

场景 3: 重启回 GPT-4o
  进程级内存清空 → 重新检测（首次发图 → 成功 → 写 "supported"）
  仅浪费一次 API 调用
```

### 2.3 三层优先级

```
1. 用户显式配置 (MEDIA_PIPELINE["main_llm_native_multimodal"] = "true"/"false")
   → 最高优先级，忽略缓存（用户明确指定）

2. 运行时检测缓存 (模块级进程内存 dict)
   → 按模型 key 查找
   → "supported" / "unsupported" — 本进程内已检测过
   → 跨 session 共享（同进程），不跨重启

3. 默认 "auto" — 未检测过
   → 尝试原生 → 成功/失败 → 写缓存
```

## 3. 数据结构

### 3.1 进程级内存缓存

**存储**: 模块级 dict，进程生命周期内有效

```python
# 结构: dict[model_key, dict[media_type, capability_value]]
_cache: dict[str, dict[str, str]] = {
    "openai/gpt-4o": {"vision": "supported", "audio": "supported", "video": "unsupported"},
    "deepseek/deepseek-chat": {"vision": "unsupported", "audio": "unsupported", "video": "unsupported"},
}
```

| 字段     | 类型 | 值                                         | 说明         |
| -------- | ---- | ------------------------------------------ | ------------ |
| 外层 key | str  | `"{provider}/{model_name}"`                | 模型身份     |
| 内层 key | str  | `"vision"` / `"audio"` / `"video"`         | 媒体类型     |
| value    | str  | `"auto"` / `"supported"` / `"unsupported"` | 原生支持能力 |

**为什么选进程内存而非文件**：

|              | 持久化文件                       | 进程内存 dict               |
| ------------ | -------------------------------- | --------------------------- |
| 跨 session   | 是                               | 是（同进程）                |
| 跨重启       | 是                               | 否（重启浪费 1 次调用）     |
| 实现复杂度   | 高（文件读写、并发安全、原子写） | 低（dict + threading.Lock） |
| 模型切换清理 | 需手动删除/编辑文件              | 重启即清空，天然干净        |

模型切换本身需要改 env + 重启，重启后进程内存天然清空，新模型从 `"auto"` 开始检测，不存在旧缓存污染。

### 3.2 Session 级 Flag（state_register_mem）

| Flag                        | 设定时机                          | 含义                                         | 生命周期            |
| --------------------------- | --------------------------------- | -------------------------------------------- | ------------------- |
| `_multimodal_trying_native` | before_agent, auto 模式尝试原生时 | 当前 turn 正在尝试原生                       | session 内，单 turn |
| `_multimodal_native_model`  | before_agent, auto 模式           | 记录当前尝试的模型 key（LLMRetry 重绑 fallback 候选时刷新为实际服务模型），用于 LLMRetry 写缓存 | session 内，单 turn |

### 3.3 配置

仓库约定「一个 feature 一个 TypedDict + 一个实例」，媒体管线配置位于
`config/features/agent_side/media_pipeline.py`（**不是** `context_engine_hook.py`——该文件不存在）。
新增 `main_llm_native_multimodal: str` 字段，默认 `"auto"`：

```python
class MediaPipelineConfig(TypedDict):
    multimodal_temp_retention_days: int
    main_llm_native_multimodal: str  # "auto" | "true" | "false"
    main_llm_silent_degradation_detection: bool
    max_media_bytes: int  # 单条媒体体积上限；超限跳过（不落盘、不进 MediaPaths）

MEDIA_PIPELINE: MediaPipelineConfig = {
    "multimodal_temp_retention_days": 7,
    "main_llm_native_multimodal": "auto",
    "main_llm_silent_degradation_detection": True,
    "max_media_bytes": 20 * 1024 * 1024,
}
```

> `main_llm_native_multimodal` 只认 `"auto"` / `"true"` / `"false"`；任何其他值
> （空串、`"yes"`、`"TRUE"` 等）都走 fail-safe 技能路径，不保留原生 block。
>
> `main_llm_silent_degradation_detection` 控制 auto 模式下的静默降级检测（见 §5.3），
> 默认开启；关闭时只清除本轮 native 尝试标记，不写能力缓存。
>
> 该字段是三态总开关，覆盖全部媒体类型（vision / audio / video），不只是 image——
> 因此命名为 `native_multimodal` 而非 `supports_vision`。
>
> `tests/config/test_features_agent_side.py` 按 `(实例, 键, 默认值)` 逐项断言配置，
> 新增字段后需同步补一行。

## 4. 改动清单

### 4.1 新增文件

#### `agent/middlewares/llm_capability_cache.py`

```python
"""LLM multimodal capability cache — process-level in-memory dict.

Key: "{provider}/{model_name}"
Values per media type: "auto" (untested) | "supported" | "unsupported"

Cache survives across sessions within a single process but is lost on
restart. Model switches require env changes + restart, so the cache is
naturally clean for the new model. The cost of one wasted API call per
restart is negligible.
"""

import os
import threading
from typing import Literal
from loguru import logger

_MediaType = Literal["vision", "audio", "video"]
_CapabilityValue = Literal["auto", "supported", "unsupported"]

_lock = threading.Lock()
_cache: dict[str, dict[str, str]] = {}


def get_capability(provider: str, model: str, media: _MediaType) -> _CapabilityValue:
    """Read cached capability for a model+media pair. Returns "auto" if unknown."""

def set_capability(provider: str, model: str, media: _MediaType, value: _CapabilityValue) -> None:
    """Write (or update) the capability cache. Thread-safe."""

def get_model_key() -> str:
    """Return "{provider}/{model_name}" for the currently configured main LLM.
    Reads MAIN_LLM_PROVIDER and MAIN_LLM_NAME env vars.
    """

def reset_cache() -> None:
    """Clear all cached capabilities (for testing or manual reset)."""
```

#### `tests/agent/middlewares/test_llm_capability_cache.py`

- 读写缓存
- 模型 key 隔离
- 未知模型返回 "auto"
- 并发写安全
- reset_cache 清空
- 进程内存特性（不跨进程，同进程跨 session 共享）

### 4.2 修改文件

#### `config/features/agent_side/media_pipeline.py`

新增 `main_llm_native_multimodal: str` 字段，默认 `"auto"`。

#### `agent/middlewares/media_pipeline/core.py`

**重构 `_before_agent_impl` 为两阶段：**

```python
def _before_agent_impl(self, state: AgentState) -> None:
    # ... session_id validation (不变) ...
    # ... get last_mes + content (不变) ...

    if not isinstance(content, list):
        return

    # Phase 1: 始终处理媒体 items（存盘 + 记录 paths）
    text_dict = None
    paths = MediaPaths()
    for item in content:
        if item.get("type") == "text":
            text_dict = item
            continue
        handler = _MEDIA_HANDLERS.get(item.get("type"))
        if handler:
            handler.process(item, session_id, paths, SRC_DIR)

    if text_dict is None:
        text_dict = {"type": "text", "text": ""}

    # Phase 2: 根据能力决定路径
    has_media = bool(paths.image_hints or paths.audios or paths.videos)
    if not has_media:
        last_mes.content = [text_dict]
        return

    native_mode = MEDIA_PIPELINE.get("main_llm_native_multimodal", "auto")

    if native_mode == "true":
        # 用户显式说支持 → 保留 block，只持久化 media paths
        self._persist_media_kwargs(last_mes, paths)
        return  # content 保持原样（含 image_url blocks）

    if native_mode == "false":
        # 用户显式说不支持 → 当前行为
        self._attach_media_hints(text_dict, paths)
        last_mes.content = [text_dict]
        self._persist_media_kwargs(last_mes, paths)
        self._strip_history_images(state_mes_list)
        return

    # native_mode == "auto"
    # 按媒体类型查缓存
    model_key = get_model_key()
    provider, _, model = model_key.partition("/")
    vision_cap = get_capability(provider, model, "vision")
    audio_cap = get_capability(provider, model, "audio")
    video_cap = get_capability(provider, model, "video")

    all_supported = True
    trying_native = False

    if paths.image_hints and vision_cap == "unsupported":
        all_supported = False
    elif paths.image_hints and vision_cap == "auto":
        trying_native = True

    if paths.audios and audio_cap == "unsupported":
        all_supported = False
    elif paths.audios and audio_cap == "auto":
        trying_native = True

    if paths.videos and video_cap == "unsupported":
        all_supported = False
    elif paths.videos and video_cap == "auto":
        trying_native = True

    if trying_native and all_supported:
        # 尝试原生：保留 block + 设 flag
        state_register_mem.set_state(session_id, "_multimodal_trying_native", True)
        state_register_mem.set_state(session_id, "_multimodal_native_model", model_key)
        self._persist_media_kwargs(last_mes, paths)
        return  # content 保持原样

    # 全部已确认不支持，或有混合（部分支持/部分不支持）
    # → 走技能路径（当前行为）
    self._attach_media_hints(text_dict, paths)
    last_mes.content = [text_dict]
    self._persist_media_kwargs(last_mes, paths)
    self._strip_history_images(state_mes_list)
```

**重构 `_before_agent_impl` 的现状对齐要点**（照现状实现，勿按上面伪码字面照抄）：

- 保留 `raise Exception("Only one text item allowed per input list")` 校验（现状 `core.py:68`），伪码里省略了它——它是既有契约，不能被本次重构删掉。
- 现状**没有** `_strip_history_images` 方法：历史 `image_url` 剥离逻辑内联在 `_before_agent_impl`（`core.py:82-99`，配 `_strip_image_url_from_content` 静态助手）。重构时把这段原样保留（可抽成同名私有方法），不要凭伪码新造调用。
- 非 dict 的 content item 现状是 `continue` 跳过（`core.py:64-65`），保持不变。

**重构 `_attach_media_hints`** — 去掉硬编码 "model has no native vision"：

```python
@staticmethod
def _attach_media_hints(text_dict: dict, paths: MediaPaths) -> None:
    if paths.image_hints:
        text_dict["text"] += (
            f"\n[Uploaded media] {len(paths.image_hints)} image(s). "
            f"Location: {','.join(paths.image_hints)}. "
            "Use skill_view('image_to_text') to read SKILL.md, then follow "
            "the skill's terminal script to recognize the image(s). "
            "The skill script result is ground truth."
        )
    # audio/video 同理...
```

**新增模块级函数 `apply_skill_fallback`：**

```python
def apply_skill_fallback(messages: list[BaseMessage], session_id: str) -> list[BaseMessage]:
    """Strip media blocks from the last HumanMessage and attach skill hints.

    Called by LLMRetryMiddleware when a model call fails with
    multimodal_not_supported during an auto-mode native attempt.
    Returns a new messages list with the modified last message.
    """
    if not messages:
        return messages
    last_mes = messages[-1]
    if not isinstance(last_mes, HumanMessage):
        return messages
    content = getattr(last_mes, "content", None)
    if not isinstance(content, list):
        return messages

    # Reconstruct text + paths from existing content
    text_dict = None
    paths = MediaPaths()
    for item in content:
        if isinstance(item, dict):
            if item.get("type") == "text":
                text_dict = item
            else:
                handler = _MEDIA_HANDLERS.get(item.get("type"))
                if handler:
                    handler.process(item, session_id, paths, SRC_DIR)

    if text_dict is None:
        text_dict = {"type": "text", "text": ""}

    # Attach skill hints + replace content
    MultimodalProcessor._attach_media_hints(text_dict, paths)
    # Create a new HumanMessage with the fallback content
    new_last = HumanMessage(content=[text_dict])
    # Copy additional_kwargs for media persistence
    new_last.additional_kwargs = dict(getattr(last_mes, "additional_kwargs", {}) or {})
    return messages[:-1] + [new_last]
```

**实现要点（避免重复落盘）**：`apply_skill_fallback` 重建 `MediaPaths` 时**优先复用
`last_mes.additional_kwargs` 里已记录的 `images` / `audios` / `videos` 路径**，只有当它们缺失时
才回退到 `handler.process(...)` 重新处理 content。原因是 `_before_agent_impl` 在 auto 模式下
**已经**对同一批 content 调过一次 `handler.process`（存盘 + 写 additional_kwargs）；若 fallback
再次 `process` 一遍 `data:`/base64 载荷，会重复解码并产生第二份临时文件，且 `paths` 与已持久化的
`additional_kwargs` 路径不一致。「技能提示文案用重建的 paths、持久化用原 kwargs」两侧必须自洽。

#### `pub/func/message/llm_error_classifier.py`

新增枚举值 + 模式匹配：

```python
class FailoverReason(enum.Enum):
    ...
    multimodal_not_supported = "multimodal_not_supported"
```

**精确模式匹配**（避免误匹配普通 "image" 关键词）：

```python
_MESSAGE_PATTERNS[FailoverReason.multimodal_not_supported] = (
    "unsupported content type",
    "unrecognized content type",
    "content type.*not supported",
    "image input not supported",
    "multimodal.*not supported",
    "vision.*not supported",
    "does not support.*image",
    "does not support.*audio",
    "does not support.*video",
    "invalid.*image_url",
    "image_url.*not.*valid",
    "unsupported.*image",
    "unsupported.*audio",
    "unsupported.*video",
)
```

Recovery matrix:

```python
_RECOVERY_MATRIX[FailoverReason.multimodal_not_supported] = dict(
    retryable=True, should_compress=False, should_fallback=False
)
```

`_match_special_cases` 中增加：400 + 上述关键词 → `multimodal_not_supported`（优先于普通 `format_error`）。

#### `agent/middlewares/llm_retry/core.py`

在 `awrap_model_call` / `wrap_model_call` 的 except 分支中新增：

```python
# 在 classified 分类之后，should_compress 检查之前或之后：
if classified.reason == FailoverReason.multimodal_not_supported:
    rebound = self._try_multimodal_fallback(request, session_id)
    if rebound is not None:
        request = rebound
        retry_count = 0
        continue
    # No fallback available → re-raise
    raise
```

新增方法：

```python
def _try_multimodal_fallback(
    self, request: ModelRequest, session_id: str
) -> ModelRequest | None:
    """Fall back from native multimodal to the skill-based path.

    Only triggers when the session is in _trying_native mode (auto config).
    Writes the detection result to the in-memory capability cache, then
    rewrites the request's messages to strip media blocks and attach
    skill hints.
    """
    trying = state_register_mem.get_state(session_id, "_multimodal_trying_native", False)
    if not trying:
        return None

    model_key = state_register_mem.get_state(session_id, "_multimodal_native_model", "")
    state_register_mem.set_state(session_id, "_multimodal_trying_native", False)

    if model_key:
        provider, _, model = model_key.partition("/")
        # Determine which media types were present in this message
        messages = getattr(request, "messages", [])
        media_types = _detect_media_types_in_messages(messages)
        for mt in media_types:
            set_capability(provider, model, mt, "unsupported")

    # Rewrite messages: strip media blocks, add skill hints
    from agent.middlewares.media_pipeline.core import apply_skill_fallback
    messages = getattr(request, "messages", [])
    new_messages = apply_skill_fallback(messages, session_id)
    try:
        return request.override(messages=new_messages)
    except Exception as exc:
        logger.error("Failed to override messages for multimodal fallback: {}", exc)
        return None
```

**fallback 归属**：`LLMRetryMiddleware` 因故障重绑到 fallback 候选时
（`_apply_sticky_fallback` 与 `_try_fallback` 两条路径），若当前 turn 处于 native
尝试（`_multimodal_trying_native` 为真），先把 `_multimodal_native_model` 刷新为
`"{candidate.provider}/{candidate.model_name}"`（实际服务模型）。这样候选模型拒绝媒体时，
`unsupported` 记在候选模型名下，env 主模型保持 `auto`（§2.2 场景 2）；非 native 尝试时
重绑不写该 key。

#### `agent/middlewares/__init__.py`

导出 `llm_capability_cache` 模块。

### 4.3 测试覆盖

| 文件                                                          | 测试点                                                                                                                                |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/agent/middlewares/test_llm_capability_cache.py` (新增) | 读写缓存、模型 key 隔离、未知模型返回 "auto"、并发写安全、reset、同进程跨 session 共享                                                |
| `tests/agent/middlewares/test_multimodal_processor.py` (扩展) | 三态分支（true/false/auto）、auto+缓存=supported 保留 block、auto+缓存=unsupported 走技能、auto+缓存=auto 设 flag、各媒体类型独立判断、**既有用例全部保持通过** |
| `tests/pub/func/message/test_llm_error_classifier.py` (扩展)  | `multimodal_not_supported` 模式匹配、不误匹配普通 "image" 关键词、400 + 关键词组合                                                    |
| `tests/agent/middlewares/test_llm_retry.py` (扩展)            | 模型报 multimodal 错误 → 写缓存 → fallback → retry 成功、非 auto 模式不触发 fallback、模型 key 校验                                   |

## 5. 边界情况

### 5.1 模型切换

| 场景                                 | 处理                                                                                                    |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| 用户从 GPT-4o 切换到 deepseek-chat   | 改 env + 重启 → 进程内存清空 → 新模型从 "auto" 开始检测                                                 |
| 用户从 deepseek 切回 GPT-4o          | 同上，重启后重新检测（浪费 1 次调用）                                                                   |
| 用户改了 provider 但 model_name 相同 | key 含 provider → 视为新模型                                                                            |
| Fallback chain 模型不支持 vision     | LLMRetry 切到 fallback → 报 multimodal 错 → 写 fallback 模型缓存到进程内存 → 走技能；同进程后续命中缓存 |

### 5.2 混合媒体

一条消息同时有图片 + 视频：

```
图片: 缓存 = "supported"   → 保留 image_url block
视频: 缓存 = "unsupported" → 剥离 video block + 加视频技能提示
```

`apply_skill_fallback` 和 `_before_agent_impl` 需要支持**部分剥离**：保留支持的 block，只剥离不支持的。

**已实现部分剥离**：`MultimodalProcessor._should_keep_native` 只在**全部**在途媒体族
unsupported 时整体走技能路径；混合（部分 supported、部分 unsupported）保留原生块，
由新增的 `wrap_model_call` / `awrap_model_call` 能力擦洗层在每次模型请求上把 unsupported
族的块替换为文本占位符（含落盘路径与技能提示），只改请求副本、不动 state/checkpointer/MesMemory。
`apply_skill_fallback` 仍是模型整体拒绝时的整条改写路径。

### 5.3 静默降级

模型不报错但忽略图片 → 如果放任不管，缓存不更新 → 下次仍尝试原生。

已实现**精确率优先**的启发式检测：`agent/middlewares/media_pipeline/degradation.py`
的 `detect_media_blindness(text)`（纯正则、大小写不敏感、不调用 LLM），多语匹配
（en / zh / ja / ko）模型**自述无法感知媒体**（如 `cannot see the image` / 无法查看图片 /
画像を見ることができません / 이미지를 볼 수 없습니다），以及**明确请求用户描述所附媒体**
（如 `please describe the image` / 请描述一下图片 / 画像を説明して / 이미지를 설명해）。

检测信号是「模型自述盲」而非计划字面版的「回复未提及图片内容」：字面启发式假阳性过高
（`The cat is orange` 明明看到了却没有媒体关键词），会把有能力的模型误标为盲、静默劣化质量。
自述信号精确率高、可测试，代价是召回率有限——模型既不报错、也不自述盲、只是默默忽略媒体时
仍检测不到（见 §7）。

挂接点在 `LLMRetryMiddleware` 的**成功路径**（`LLMRetry` 收到结果之后、partial-stub 处理之前）：
仅当本轮处于 auto 模式 native 尝试（`_multimodal_trying_native`）且开关
`main_llm_silent_degradation_detection` 为真时评估；命中则对**实际服务模型**（沿用 fallback
重绑后的归属语义）在消息内**实际出现的媒体族**写 `"unsupported"` 并记 warning。无论是否命中，
评估后都清除 `_multimodal_trying_native`，避免陈旧 flag 影响同轮后续调用。既有的
`multimodal_not_supported` 错误分支语义不变。

### 5.4 缓存重置

| 方式                                    | 效果                              |
| --------------------------------------- | --------------------------------- |
| 重启服务器                              | 进程内存清空，所有模型回到 "auto" |
| 调用 `reset_cache()`                    | 清空进程内存 dict（测试用）       |
| 设 `main_llm_native_multimodal = "true"`  | 覆盖缓存，始终保留 block          |
| 设 `main_llm_native_multimodal = "false"` | 覆盖缓存，始终走技能              |

### 5.5 request.override(messages=...) API 验证

**已验证支持**：LangChain 1.3.9 的 `ModelRequest.override(messages=...)` 已在生产路径中使用——
`Summarization`（压缩后替换 messages）与 `ContextEvictionMiddleware`（P1-9 人以 preview 替换）都走
`request.override(messages=...)`；`LLMRetryMiddleware._rebind_model` 用的是同一 API 的
`override(model=...)` 形式。无需降级方案。

## 6. 实现顺序

1. `llm_capability_cache.py` — 进程内存缓存读写模块 + 测试
2. `llm_error_classifier.py` — 新增 `multimodal_not_supported` 分类 + 测试
3. `media_pipeline/core.py` — 重构 `_before_agent_impl` + 新增 `apply_skill_fallback` + 测试
4. `llm_retry/core.py` — 新增 multimodal fallback 分支 + 测试
5. `media_pipeline.py`（配置）— 新增 `main_llm_native_multimodal` 字段 + 同步 `tests/config/test_features_agent_side.py` 的逐键断言
6. 端到端验证：auto 模式 → 发图 → 模型报错 → 写缓存 → fallback → 同进程后续 session 直接走技能

## 7. 已知限制

- **静默降级**：仅当模型自述无法感知媒体（或明确要求用户描述媒体）时才检测到；模型既不报错、
  也不自述、只是默默忽略媒体时仍检测不到，缓存保持 `auto`（下一轮继续尝试原生）。
- **错误模式匹配**：不同 provider 报错措辞不一，可能漏匹配。首轮不触发，后续迭代补充模式。
- **重启重新检测**：进程内存不跨重启，重启后首次发图浪费 1 次调用。代价可忽略。
- **混合媒体**：已实现按块部分剥离（见 §5.2）；模型整体拒绝时仍由 `apply_skill_fallback` 整条改写。
- **ModelRequest.override API**：已验证支持 `messages` 覆盖（见 §5.5），无降级路径。
- **总开关而非按类型**：`main_llm_native_multimodal` 是覆盖全部媒体类型（vision / audio / video）
  的三态总开关；若未来要「vision 原生、audio 走技能」这类按类型配置，需要扩展为每类型一份配置。
