# 计划：思考等级全模型适配（学 pi 声明式 + 前端用户自配）

- 建档日期：2026-10-14
- 状态：**部分执行（2026-10-15）**——Phase 1 (P0) 与 Phase 2 (P1) 的声明式主体已落地，
  Phase 3 (P2，前端自配 wire 属性名面板) 未开工。落地明细见文末「落地记录」。

- 触发问题：Sherry 当前思考等级适配仅覆盖 4 类 provider（DeepSeek / Anthropic budget /
  OpenAI reasoning_effort / GLM thinking），3 级枚举（low / high / max），硬编码 if/else
  Strategy 链，不支持 Anthropic adaptive、Google 双格式、Bedrock，always-think 处理仅
  GLM 一家。pi 项目已实现了 13+ provider、7 级枚举、声明式 `thinkingLevelMap` + 11 种
  `thinkingFormat` 的完整适配体系，远超 Sherry。同时用户希望在前端能自选思考等级值
  名称（low / medium / high），并能在必要时自定义 wire 属性名以适配任意 provider。

---

## 0. 结论先行

| 目标 | 方案 | 优先级 |
|---|---|---|
| 后端：声明式 thinkingLevelMap 取代 if/else Strategy | 用 per-model 映射表 + thinkingFormat 选择器 + clampThinkingLevel 统一入口 | P0 |
| 后端：枚举从 3 级扩展到 7 级 | off + minimal / low / medium / high / xhigh / max | P0 |
| 后端：provider 覆盖从 4 扩到 13+ | 新增 Anthropic adaptive / Google 双格式 / Bedrock / Qwen / Together / OpenRouter / Baseten 等 | P1 |
| 前端：统一等级选择器 | 从 on_off/levels 双模式 → 统一的 7 级下拉（含 off） | P0 |
| 前端：用户自配 wire 属性名 | 高级面板：per-provider 自定义 wire 字段名 + 值映射 | P2 |

**关键约束**：不引入对 `server/**` 的 import（agent 层 + models 层改动为主）；
config 层新增 TypedDict（遵守 `config/features/**` 不 import agent/server/models 的规则）；
前端改动不破坏现有 park/promote twin 和 HITL 流程。

---

## 1. 现状对比：Sherry vs pi

### 1.1 架构对比

| 维度 | Sherry 现状 | pi |
|---|---|---|
| 枚举级别 | off / low / high / max（4 个） | off + minimal / low / medium / high / xhigh / max（7 个） |
| 映射方式 | 硬编码 `_ReasoningStrategy` if/else 链（`reasoning_payload.py`） | 声明式 `thinkingLevelMap`（per-model JSON） + `thinkingFormat` 选择器 |
| provider 覆盖 | 4 类（DeepSeek / Anthropic budget / OpenAI reasoning_effort / GLM） | 13+（DeepSeek / Anthropic 三模式 / OpenAI Responses+Completions / Z.AI / Google 双格式 / Bedrock Claude+GPT / Qwen / Together / OpenRouter / Baseten / ant-ling / chat-template / string-thinking） |
| clamp 钳位 | 无（直接发请求，失败才 fallback 到 floor） | `clampThinkingLevel`：先向上后向下找最近支持 level |
| always-think 处理 | GLM only（`_ALWAYS_THINK_PREFIXES`） | 统一 `off: null` 语义（null = 不支持禁用） |
| Anthropic | 仅 budget_tokens | 三模式：managed-effort / adaptive / budget |
| Google | ❌ NoOp | ✅ 离散 thinkingLevel + token thinkingBudget 双格式 |
| Bedrock | ❌ | ✅ Claude adaptive + GPT reasoning_effort |
| wire 格式选择器 | ❌ | ✅ 11 种 `thinkingFormat` |
| 前端控制 | 双模式（on_off switch / levels 低高最高） | — |
| 前端自配 wire | ❌ | ❌（pi 也不支持，这是 Sherry 独有需求） |

### 1.2 Sherry 关键代码路径

```
ThinkingToggle.vue (前端)
  → useThinkingStore.setValue(sid, value)
  → PUT /sessions/thinking {session_id, value}
  → session_settings.py::apply_thinking_choice()
  → state_register (LLM_THINKING_ENABLED, mem + SQLite, park/promote twin)

ThinkingControlMiddleware.core.py (模型调用时)
  → 读 LLM_THINKING_ENABLED → _cache_key_for() → "on"/"off"/"off_floor"/"level:X"
  → build_main_llm(thinking=True/False, thinking_level="low"/"high"/"max", thinking_floor=True)
  → _reasoning_for_state() → reasoning_payload.py 分发
  → _strategy_for(provider).build() / .disable() / thinking_budget()
  → NormalizingChatModel 包裹 → 流式 reasoning_content
```

### 1.3 pi 关键代码路径

```
Model.thinkingLevelMap (per-model 声明: {off: null, low: "low", high: "high", ...})
Model.compat.thinkingFormat (11 种选择器: "deepseek" / "zai" / "openai" / ...)

clampThinkingLevel(model, level) → 先向上后向下找最近支持 level
  → getSupportedThinkingLevels(model): filter EXTENDED_THINKING_LEVELS by map[level] !== null

adapter.buildParams():
  → switch(compat.thinkingFormat):
    → "deepseek": thinking={type:"enabled"} + reasoning_effort=map[level]??level
    → "zai": thinking={type:"enabled",clear_thinking:false} + reasoning_effort=...
    → "openai": reasoning_effort=map[level]??level
    → "openrouter": reasoning={effort:map[level]??level}
    → "together": reasoning={enabled:bool} + reasoning_effort=...
    → "qwen": enable_thinking=bool + reasoning_effort=...
    → "baseten": chat_template_args + reasoning_effort=...
    → "chat-template": chat_template_kwargs via $var placeholders
    → "string-thinking": thinking=string
    → "ant-ling": reasoning={effort} only when mapped
  → 独立 token budget cap (thinkingTokenBudgetField)
  → resolveSamplingParams (per-level 采样参数)

Anthropic adapter:
  → forceAdaptiveThinking → thinking={type:"adaptive"} + output_config={effort}
  → supportsMidConvoEffort → thinking={type:"adaptive",block_binding} + 系统消息注入 effort
  → 旧版 → thinking={type:"enabled",budget_tokens}

Google adapter:
  → usesGoogleThinkingLevel → thinkingConfig={thinkingLevel: MINIMAL/LOW/MEDIUM/HIGH}
  → 旧版 → thinkingConfig={thinkingBudget: token数}

Bedrock adapter:
  → Claude adaptive → additionalModelRequestFields.thinking={type:"adaptive"} + output_config
  → GPT → reasoning={effort} 或 flat reasoning_effort
```

### 1.4 差异根因

Sherry 的 `reasoning_payload.py` 是 **provider-keyed dispatch**（按 provider 名选 Strategy
类），每个 Strategy 内部用 model_name prefix 做二次 if/else。这意味着：

1. **加一个 provider 要改 3+ 处**：`_OPENAI_COMPATIBLE` 集合、Strategy 类、`thinking_control_mode`
2. **加一个 level 要改枚举 + 所有 Strategy 的 build()**：没有声明式映射，每个 provider 硬编码
3. **clamp 不存在**：用户选了不支持的 level 直接发错 wire 值，靠 400 拒绝后 fallback
4. **always-think 只认 GLM**：`_ALWAYS_THINK_PREFIXES` 硬编码，新 provider 加不了

pi 的 `thinkingLevelMap` 是 **model-keyed dispatch**（每个模型声明自己的 wire 映射），
adapter 只认 `thinkingFormat`（wire 格式选择器），新增 provider 只需加一个 format 分支 +
在模型目录里填映射表。

---

## 2. 方案 A：后端声明式 thinkingLevelMap 适配（P0）

### 2.1 新增枚举与类型（`config/features/agent_side/`）

**新文件：`config/features/agent_side/thinking_levels.py`**

```python
class ThinkingLevel(str, Enum):
    """pi-style 7-level thinking enum (off + 6 effort levels)."""
    OFF = "off"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"

THINKING_LEVELS: tuple[str, ...] = (
    "minimal", "low", "medium", "high", "xhigh", "max"
)
EXTENDED_THINKING_LEVELS: tuple[str, ...] = (
    "off", "minimal", "low", "medium", "high", "xhigh", "max"
)

class ThinkingLevelMap(TypedDict, total=False):
    """Per-model wire-value declarations.

    Key = pi level, Value = wire string | None.
    - string: the exact wire value to send for this level
    - None: this level is UNSUPPORTED (clamp will skip it)
    - omitted: use the adapter's built-in default mapping for this level
    """
    off: str | None
    minimal: str | None
    low: str | None
    medium: str | None
    high: str | None
    xhigh: str | None
    max: str | None

class ThinkingFormat(str, Enum):
    """Wire format selector for OpenAI-compatible providers."""
    OPENAI = "openai"
    DEEPSEEK = "deepseek"
    ZAI = "zai"            # Z.AI / GLM
    QWEN = "qwen"
    TOGETHER = "together"
    BASETEN = "baseten"
    OPENROUTER = "openrouter"
    CHAT_TEMPLATE = "chat-template"
    STRING_THINKING = "string-thinking"
    ANT_LING = "ant-ling"

class ThinkingConfig(TypedDict):
    """Aggregated thinking-level configuration."""
    levels: tuple[str, ...]               # THINKING_LEVELS
    extended_levels: tuple[str, ...]      # EXTENDED_THINKING_LEVELS
    default_level: str                    # "high"
    default_effort: str                   # OpenAI fallback: "high"
    min_answer_tokens: int                # 1024 — pi's MIN_ANSWER_TOKENS
```

**修改：`config/features/agent_side/reasoning_budget.py`**
- 保留现有 `ReasoningBudgetConfig`，新增 `thinking_budgets: dict[str, int]` 字段
  （per-level token budget，对标 pi 的 `DEFAULT_THINKING_BUDGETS`）

### 2.2 重写 reasoning_payload.py（声明式分发）

**核心改动：用 `ThinkingLevelMap` + `ThinkingFormat` 取代 `_ReasoningStrategy` if/else 链**

```
reasoning_payload.py 新结构:

# ── 模型目录：per-provider 的 thinkingLevelMap 模板 ──
# 对标 pi 的 generate-models.ts 硬编码映射 + getEffortThinkingLevelMap 泛化构建器

_THINKING_LEVEL_MAPS: dict[str, ThinkingLevelMap] = {
    # DeepSeek V3.2+
    "deepseek": {"off": "disabled", "low": "low", "medium": "medium", "high": "high", "max": "max"},
    # GLM-5 (always-think: off 不支持)
    "glm-5": {"off": None, "low": "low", "high": "high", "max": "max"},
    # GLM-4.5/4.6 (支持禁用)
    "glm-4": {"off": "disabled", "low": "low", "high": "high", "max": "max"},
    # GPT-5/o-series (不支持 off)
    "openai-reasoning": {"off": None, "minimal": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"},
    # Anthropic adaptive (不支持 off)
    "anthropic-adaptive": {"off": None, "minimal": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"},
    # Anthropic budget (不支持 off，默认关)
    "anthropic-budget": {"off": None, "low": "low", "medium": "medium", "high": "high"},
    # Google Gemini 3+ (不支持 off)
    "google-level": {"off": None, "minimal": "MINIMAL", "low": "LOW", "medium": "MEDIUM", "high": "HIGH"},
    # Google 旧版 (不支持 off)
    "google-budget": {"off": None, "low": "low", "medium": "medium", "high": "high"},
    # ...
}

# ── wire 格式分发器 ──
# 对标 pi 的 openai-completions.ts buildParams switch

def build_thinking_kwargs(
    provider: str | None,
    model_name: str | None,
    level: str,                    # "off"|"minimal"|...|"max"
    *,
    thinking_format: str | None = None,
    thinking_level_map: ThinkingLevelMap | None = None,
) -> dict[str, Any]:
    ...
```

**新增函数：**

```python
def get_supported_thinking_levels(
    provider: str | None,
    model_name: str | None,
) -> list[str]:
    """对标 pi::getSupportedThinkingLevels — filter EXTENDED_THINKING_LEVELS by map[level] is not None.
    xhigh/max 额外要求显式声明（非 undefined）。"""

def clamp_thinking_level(
    provider: str | None,
    model_name: str | None,
    level: str,
) -> str:
    """对标 pi::clampThinkingLevel — 先向上后向下找最近支持 level。
    替代当前 ThinkingControlMiddleware 的 _off_floor_learned fallback。"""

def resolve_thinking_format(
    provider: str | None,
    model_name: str | None,
) -> str | None:
    """自动检测 wire 格式（对标 pi 的 compat 自动检测）。
    deepseek → "deepseek"; zhipu → "zai"; dashscope → "qwen";
    openrouter → "openrouter"; 其他 OpenAI-compatible → "openai"。"""

def resolve_thinking_level_map(
    provider: str | None,
    model_name: str | None,
) -> ThinkingLevelMap | None:
    """根据 provider + model_name 找到对应的 thinkingLevelMap 模板。
    支持用户自定义覆盖（方案 C 的前端自配 wire 属性名时使用）。"""
```

**保留的旧函数（向后兼容 wrapper）：**
- `build_reasoning_kwargs(provider, model_name, enabled, reasoning_effort)` → 内部调 `build_thinking_kwargs(level="high" if enabled else "off")`
- `build_thinking_off_kwargs` → `build_thinking_kwargs(level="off")`
- `build_thinking_floor_kwargs` → `build_thinking_kwargs(level=clamp后最低支持)`
- `build_thinking_level_kwargs` → `build_thinking_kwargs(level=level)`
- `thinking_control_mode` → 改为基于 `get_supported_thinking_levels` 推导（有 off = "on_off"，无 off = "levels"）

### 2.3 改造 ThinkingControlMiddleware（`agent/middlewares/thinking_control/core.py`）

**关键改动：**
1. `StateKey.LLM_THINKING_ENABLED` 的值域从 `bool | "low"|"high"|"max"` 扩展到 `str | bool`（7 级 + bool 向后兼容）
2. `_cache_key_for()` 从 4 种 key 扩展到 7+1 种：`"off"` / `"level:minimal"` / `"level:low"` / ... / `"level:max"`
3. 去掉 `_off_floor_learned` 硬编码 fallback，改为调用 `clamp_thinking_level()` 一次性钳位
4. `build_main_llm(thinking_level=X)` 传完整的 level 字符串

```python
# 改前:
if cache_key and cache_key.startswith("level:"):
    return {"thinking_level": cache_key.split(":", 1)[1]}

# 改后:
if cache_key and cache_key.startswith("level:"):
    level = cache_key.split(":", 1)[1]
    # clamp 到模型实际支持的最近 level
    clamped = clamp_thinking_level(provider, model_name, level)
    return {"thinking_level": clamped}
```

### 2.4 改造 build_main_llm（`models/LLMs/main_llm.py`）

- `_reasoning_for_state()` 改为接收 `level: str`（7 级之一），不再分 `thinking: bool` / `thinking_level: str` / `thinking_floor: bool` 三个参数
- `build_main_llm(level: str | None = None)` 统一入口（`thinking` / `thinking_level` / `thinking_floor` 参数保留为 deprecated wrapper）
- `apply_thinking_budget()` 用 per-level budget 表（`thinking_budgets[level]`），不再只有 on/off 两档

### 2.5 改造 HTTP 端点与 state register

**`server/trigger/http/session_settings.py`**
- `GET /sessions/thinking` 返回 `mode` 从 `"on_off"/"levels"` 扩展到 `"unified"`（统一 7 级下拉）
- `PUT /sessions/thinking` 的 value 值域扩展到 `str`（7 级之一）+ `bool`（向后兼容）+ `null`
- 新增 `GET /sessions/thinking/levels` → 返回当前模型支持的 level 列表（对标 pi 的 `getSupportedThinkingLevels`）

**`server/service/session_settings_service.py`**
- `apply_thinking_choice()` 验证值域从 `bool | "low"|"high"|"max"` 扩展到 `str | bool | None`
- `get_thinking_mode()` 改为返回 `"unified"` 或 `"on_off"`（向后兼容旧前端）

### 2.6 新增 provider 适配（P1）

| Provider | thinkingFormat | wire 字段 | 优先级 |
|---|---|---|---|
| Anthropic adaptive | 原生 | `thinking: {type: "adaptive"}` + `output_config: {effort}` | P1 |
| Anthropic managed-effort | 原生 | `thinking: {type: "adaptive", block_binding}` + system msg 注入 | P2 |
| Google Gemini 3+ (discrete) | 原生 | `thinkingConfig: {thinkingLevel}` | P1 |
| Google 旧版 (budget) | 原生 | `thinkingConfig: {thinkingBudget}` | P1 |
| Bedrock Claude | converse | `additionalModelRequestFields.thinking` + `output_config` | P2 |
| Bedrock GPT | converse | `reasoning: {effort}` 或 flat `reasoning_effort` | P2 |
| Qwen | `"qwen"` | `enable_thinking: bool` + `reasoning_effort` | P1 |
| Together | `"together"` | `reasoning: {enabled}` + `reasoning_effort` | P2 |
| OpenRouter | `"openrouter"` | `reasoning: {effort}` | P1 |
| Baseten | `"baseten"` | `chat_template_args` + `reasoning_effort` | P2 |
| chat-template (vLLM/SGLang) | `"chat-template"` | `chat_template_kwargs` with `$var` | P2 |

### 2.7 测试

- `tests/models/test_reasoning_payload.py`：
  - `get_supported_thinking_levels` per-model 正确性
  - `clamp_thinking_level` 向上/向下钳位逻辑
  - 每个 `thinkingFormat` 的 wire 输出
  - `off: null` → always-think 钳位
  - 向后兼容：旧 `build_reasoning_kwargs(enabled=True)` → `level="high"`
- `tests/agent/middlewares/test_thinking_control.py`：
  - 7 级 cache key 映射
  - clamp 后的 model variant
  - 删除 `_off_floor_learned` 后的 fallback 行为
- `tests/server/test_session_settings.py`：
  - `PUT /sessions/thinking` value = "minimal" / "xhigh" / "max"
  - `GET /sessions/thinking/levels` 返回值

---

## 3. 方案 B：前端统一等级选择器（P0）

### 3.1 目标

从当前双模式（`on_off` switch / `levels` 低高最高）改为 **统一 7 级下拉**，
用户看到的是：关闭 / 最低 / 低 / 中 / 高 / 超高 / 最高（i18n 四语）。
后端按模型实际支持的级别过滤可选项。

### 3.2 改造 ThinkingToggle.vue

```vue
<!-- 改前：双模式 menuItems computed -->
<!-- 改后：统一级别列表，后端驱动可选项 -->

<script setup>
const props = defineProps<{ sessionId: string }>();
const store = useThinkingStore();

// 从后端获取当前模型支持的级别列表
const availableLevels = computed(() => store.availableLevels(props.sessionId));

const menuItems = computed(() => {
  // 统一渲染所有支持的级别，不再分 on_off / levels
  return availableLevels.value.map(level => ({
    label: t(`thinkingToggle.${level}`),  // off/minimal/low/medium/high/xhigh/max
    command: () => store.setValue(props.sessionId, level),
  }));
});
</script>
```

**i18n 新增 key（zh/en/ja/ko）：**

| key | zh | en | ja | ko |
|---|---|---|---|---|
| `thinkingToggle.off` | 关闭 | Off | オフ | 끄기 |
| `thinkingToggle.minimal` | 最低 | Minimal | 最小 | 최소 |
| `thinkingToggle.low` | 低 | Low | 低 | 낮음 |
| `thinkingToggle.medium` | 中 | Medium | 中 | 중간 |
| `thinkingToggle.high` | 高 | High | 高 | 높음 |
| `thinkingToggle.xhigh` | 超高 | Extra High | 超高 | 특상 |
| `thinkingToggle.max` | 最高 | Max | 最高 | 최고 |

### 3.3 改造 useThinkingStore

```typescript
// 改前
export type ThinkingMode = 'on_off' | 'levels';
export type ThinkingValue = boolean | 'low' | 'high' | 'max';

// 改后
export type ThinkingMode = 'unified' | 'on_off' | 'levels';  // on_off/levels 向后兼容
export type ThinkingValue = ThinkingLevel | boolean;  // boolean 向后兼容 → true="high", false="off"
export type ThinkingLevel = 'off' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max';

state: {
  mode: ThinkingMode;
  bySession: Record<sid, ThinkingValue>;
  availableLevelsBySession: Record<sid, ThinkingLevel[]>;  // 新增：后端驱动的可选项
  pendingBySession: Record<sid, boolean>;
  defaultLevel: ThinkingLevel;
}
```

- `hydrate(sid)` 额外调用 `GET /sessions/thinking/levels` 获取可选项
- `setValue(sid, level)` 发送 `PUT /sessions/thinking` with `value: level`（string）
- `availableLevels(sid)` 返回可选项，fallback 到全部 7 级

### 3.4 改造 session bridge

```typescript
// 新增
export interface ThinkingLevelsResponse {
  session_id: string;
  levels: ThinkingLevel[];       // 支持的级别列表
  default_level: ThinkingLevel;
  effective_level: ThinkingLevel; // clamp 后的实际级别
}

export async function fetchThinkingLevels(sessionId: string): Promise<ThinkingLevelsResponse> {
  // GET /sessions/thinking/levels
}
```

### 3.5 移动端适配

- 当前移动端是循环按钮（on_off toggle / low→high→max 循环）
- 改为：循环可用级别（`availableLevels` 的循环），不再硬编码 3 级

### 3.6 测试

- `client/app/stores/__tests__/thinking.test.ts`：
  - `availableLevels` 从后端获取
  - `setValue` 发送 7 级字符串
  - 向后兼容：旧 `true` → `"high"`，旧 `false` → `"off"`
- `client/app/pages/home/components/__tests__/ThinkingToggle.integration.test.ts`：
  - 统一 7 级下拉渲染
  - 只显示后端返回的可选级别
  - i18n 标签

---

## 4. 方案 C：前端用户自配 wire 属性名（P2）

### 4.1 目标

当 Sherry 不原生支持某个 provider 的 thinking wire 格式时，用户可以在前端的高级面板
中手动配置：
1. wire 字段路径（如 `extra_body.thinking.type` / `reasoning_effort` / `reasoning.effort`）
2. 每个 level 对应的 wire 值（如 `off → "disabled"`, `high → "high"`）

这本质上是把 pi 的 `thinkingLevelMap` + `thinkingFormat` **从代码中提到运行时配置**，
让用户自己填映射表。

### 4.2 数据结构

**后端新增（`config/features/agent_side/thinking_levels.py`）：**

```python
class CustomThinkingMapping(TypedDict, total=False):
    """用户自定义 wire 映射（per-provider 覆盖）。

    存储在 session 级别（AGENT_CONFIG 或独立 state key），
    运行时由 resolve_thinking_level_map 优先读取。
    """
    wire_field: str           # wire 字段路径，如 "extra_body.thinking.type" / "reasoning_effort"
    wire_format: str          # thinkingFormat 选择器之一，或 "custom"
    level_map: ThinkingLevelMap  # per-level wire 值
    # 可选：自定义 token budget per level
    thinking_budgets: dict[str, int]
```

**State key 新增：**

```python
# runtime/session/state_keys.py
CUSTOM_THINKING_MAPPING = "custom_thinking_mapping"              # live
CUSTOM_THINKING_MAPPING_PENDING = "custom_thinking_mapping_pending"  # parked
```

### 4.3 后端改动

**`reasoning_payload.py`：**
- `resolve_thinking_level_map()` 优先读取 session 的 `CUSTOM_THINKING_MAPPING`，其次用内置模板
- `build_thinking_kwargs()` 接收可选 `custom_mapping: CustomThinkingMapping | None` 参数

**`server/trigger/http/session_settings.py`：**
- 新增 `GET /sessions/thinking/custom` → 返回当前自定义映射
- 新增 `PUT /sessions/thinking/custom` → 写入自定义映射（验证 wire_field 非空、level_map 合法）

**`session_settings_service.py`：**
- `apply_custom_thinking_mapping()` 写入 state register（park/promote twin）

### 4.4 前端改动

**新增组件：`ThinkingCustomMappingPanel.vue`**

在设置面板（预设角色 → 代理模型 tab 或独立 tab）中新增高级配置区域：

```
┌─ 思考等级高级配置 ─────────────────────────────────┐
│                                                     │
│  Wire 字段路径: [extra_body.thinking.type         ] │
│  Wire 格式:    [custom ▼]                           │
│                                                     │
│  级别映射:                                          │
│  ┌──────────┬──────────────────────────────────┐   │
│  │ off      │ [disabled          ]或不支持(null)│   │
│  │ minimal  │ [                  ]或不支持(null)│   │
│  │ low      │ [low               ]              │   │
│  │ medium   │ [medium            ]              │   │
│  │ high     │ [high              ]              │   │
│  │ xhigh    │ [                  ]或不支持(null)│   │
│  │ max      │ [max               ]              │   │
│  └──────────┴──────────────────────────────────┘   │
│                                                     │
│  [恢复默认]  [保存]                                  │
└─────────────────────────────────────────────────────┘
```

- 用户填写 wire 字段路径 + per-level wire 值
- null 值通过清空输入框 + 勾选"不支持"实现
- 保存后写入 `PUT /sessions/thinking/custom`
- 下一次模型调用时 `resolve_thinking_level_map` 优先使用自定义映射

**前端 bridge：**

```typescript
export interface CustomThinkingMapping {
  wire_field: string;
  wire_format: string;
  level_map: Partial<Record<ThinkingLevel, string | null>>;
  thinking_budgets?: Record<string, number>;
}

export async function fetchCustomThinkingMapping(sessionId: string): Promise<CustomThinkingMapping | null> {
  // GET /sessions/thinking/custom
}

export async function setCustomThinkingMapping(sessionId: string, mapping: CustomThinkingMapping): Promise<void> {
  // PUT /sessions/thinking/custom
}
```

### 4.5 安全约束

- `wire_field` 只允许路径段字符（`a-z_.`），禁止 shell 注入
- `level_map` 的 value 只允许字母数字 + 下划线 + 短横线
- `wire_format` 必须是已知枚举之一或 `"custom"`
- 保存时验证至少一个 level 有非 null 值
- `thinking_budgets` 的值必须 > 0 且 < max_tokens

### 4.6 测试

- 后端：`resolve_thinking_level_map` 优先读 custom、fallback 到内置
- 后端：`build_thinking_kwargs` 用 custom mapping 输出正确 wire 字段
- 前端：面板渲染、保存、恢复默认
- 集成：custom mapping + clamp 联动

---

## 5. 实施顺序

```
Phase 1 (P0): 后端声明式重构 + 前端统一选择器
  ├─ 1a. config/features/agent_side/thinking_levels.py 新增枚举与 TypedDict
  ├─ 1b. reasoning_payload.py 重写为声明式分发 + clamp
  ├─ 1c. ThinkingControlMiddleware 改用 clamp_thinking_level
  ├─ 1d. build_main_llm 统一 level 参数
  ├─ 1e. HTTP 端点扩展 value 值域 + GET /thinking/levels
  ├─ 1f. 前端 ThinkingToggle 统一 7 级下拉
  ├─ 1g. 前端 useThinkingStore + bridge 改造
  └─ 1h. 测试（后端 + 前端）

Phase 2 (P1): 新增 provider 适配
  ├─ 2a. Anthropic adaptive 模式
  ├─ 2b. Google 双格式（discrete + budget）
  ├─ 2c. OpenRouter / Qwen
  ├─ 2d. Together / Baseten / chat-template
  ├─ 2e. Bedrock (Claude + GPT)
  └─ 2f. 测试

Phase 3 (P2): 前端自配 wire 属性名
  ├─ 3a. 后端 CustomThinkingMapping + state key
  ├─ 3b. resolve_thinking_level_map 优先读 custom
  ├─ 3c. HTTP 端点 GET/PUT /thinking/custom
  ├─ 3d. 前端 ThinkingCustomMappingPanel.vue
  ├─ 3e. 前端 bridge + store
  └─ 3f. 测试 + 安全约束
```

---

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| 向后兼容：旧 session 的 `LLM_THINKING_ENABLED = true/false` | `build_main_llm` 保留 `thinking: bool` 参数作为 deprecated wrapper，内部映射 `True→"high"`, `False→"off"` |
| 向后兼容：旧前端发送 `value: true/false` | `apply_thinking_choice` 接受 bool，映射后存储为 level 字符串 |
| clamp 误判：模型目录不全导致 clamp 到 "off" | `clamp_thinking_level` fallback 到 `"off"` 而非 crash；同时 `get_supported_thinking_levels` 无映射时返回 `["off"]`（对标 pi） |
| 前端 i18n 遗漏 | 7 级 × 4 语 = 28 个新 key，建一个 i18n 测试 pin |
| 自配 wire 注入 | `wire_field` 白名单字符（`[a-z_.]`），value 白名单（`[a-zA-Z0-9_-]`），禁止特殊字符 |
| config 层不 import agent/server | `thinking_levels.py` 纯 TypedDict + Enum，不 import 任何上层模块 |
| import-linter 违规 | 新增文件路径在 `config/features/agent_side/` 和 `models/LLMs/`，不跨 forbidden boundary |
| subagent thinking 继承 | `agent/tools/subagent/spawn/thinking.py` 的 `resolve_thinking_override` 需适配新 level 枚举 |

---

## 7. 不做什么

- **不改 middleware chain**：ThinkingControlMiddleware 仍在 `ALWAYS_ON_MIDDLEWARES`，位置不变
- **不改 park/promote twin**：`_SETTINGS_KEYS` 增项但不改机制
- **不改 NormalizingChatModel**：reasoning_content 归一化逻辑不变
- **不改 ChatThinkingBlock.vue**：reasoning 展示组件不变
- **不改 subagent spawn 机制**：只适配 thinking level 枚举
- **不做 pi 的 models.dev 自动目录**：Sherry 手动维护 `_THINKING_LEVEL_MAPS`，不拉外部元数据
- **不做 pi 的 `supportsMidConvoEffort`（managed-effort 系统消息注入）**：P2 之后评估

---

## 8. pi 全部跨提供商适配字段盘点（思考等级之外）

> **关键发现**：pi 对 `repetition_penalty` / `frequency_penalty` / `presence_penalty` /
> `top_p` / `top_k` / `min_p` / `typical_p` / `top_a` / `seed` / `stop` 等采样参数
> **不做任何跨 provider 适配**。它们不是命名字段，只存在于 `samplingParams`
> 逃生舱——一个 `Record<string, unknown>` 自由式 dict，且**仅 OpenAI-compatible 三家
> adapter（openai-completions / openai-responses / azure-openai-responses）应用它**，
> 原生 adapter（Anthropic / Google / Bedrock / Mistral）完全忽略。

### 8.1 pi 的命名跨 provider 字段（小而精的核心集）

pi 的设计哲学是**薄归一化层**：只统一每个 chat provider 都有的少数几个参数，
其余全部交给逃生舱或不做。

| 字段 | 类型 | 跨 provider 适配方式 | pi 位置 |
|---|---|---|---|
| `temperature` | `number?` | 命名字段。OpenAI → `params.temperature`；Google → `generationConfig.temperature`；Anthropic → 有条件门控（`supportsTemperature` compat，Opus 4.7+ 拒绝非默认值；thinking 开启时也抑制） | `types.ts:224` |
| `maxTokens` | `number?` | 命名字段 + compat 选 wire 字段名：`max_tokens` / `max_completion_tokens`（OpenAI compat）/ `max_output_tokens`（Responses）/ `maxOutputTokens`（Google）/ `max_tokens`（Anthropic/Mistral）/ `maxTokens`（Bedrock） | `types.ts:233` + `compat.maxTokensField` |
| `toolChoice` | `"auto"\|"none"` | 命名字段。base 只有 auto/none，各 adapter 自行扩展更丰富变体 | `types.ts:371` |
| `cacheRetention` | `"none"\|"short"\|"long"` | 命名字段。OpenAI → `prompt_cache_key` + `prompt_cache_retention`；Anthropic → `cache_control: {type:"ephemeral", ttl}`；Mistral → `prompt_cache_key`；Google 不映射 | `types.ts:243` |
| `sessionId` | `string?` | 驱动 prompt cache key + session affinity headers | `types.ts:249` |
| `transport` | `"sse"\|"websocket"\|"auto"` | 流式传输偏好 | `types.ts:245` |
| `reasoning` | `ThinkingLevel` | 7 级思考等级 → `thinkingLevelMap` + `thinkingFormat` 适配（本计划主题） | `types.ts:372` |
| `timeoutMs` / `maxRetries` / `maxRetryDelayMs` | `number?` | 传输层超时 + 重试 | `types.ts:202-208` |

### 8.2 pi 的 `samplingParams` 逃生舱

```typescript
// types.ts:133
export type SamplingParams = Record<string, unknown>;

// types.ts:225-232 (doc on StreamOptions.samplingParams):
// "Arbitrary sampling parameters merged into the request body as-is, after the named
//  request fields, so keys here override them. Lets custom OpenAI-compatible servers
//  (llama.cpp, vLLM, SGLang, ...) receive parameters pi does not model, e.g. top_p,
//  top_k, min_p, repetition_penalty. Merged over Model.samplingParams per key.
//  Only applied by OpenAI-compatible adapters; other APIs ignore it."
```

**三层 merge 逻辑（`simple-options.ts:24-34`）：**

```typescript
function resolveSamplingParams(model, thinkingLevel, requestParams?) {
  const effectiveThinkingLevel = clampThinkingLevel(model, thinkingLevel);
  const thinkingLevelParams = model.samplingParamsByThinkingLevel?.[effectiveThinkingLevel];
  return model.samplingParams || thinkingLevelParams || requestParams
    ? { ...model.samplingParams, ...thinkingLevelParams, ...requestParams }
    : undefined;
}
```

优先级：`Model.samplingParams`（模型默认）→ `Model.samplingParamsByThinkingLevel[level]`（per-level 覆盖）→ `StreamOptions.samplingParams`（per-request 覆盖）。

**关键限制**：只有 OpenAI-compatible 三家 adapter 做 `Object.assign(params, samplingParams)`：
- `openai-completions.ts:1003-1007`（注释："Last so model and request sampling parameters override named request fields."）
- `openai-responses.ts:382-386`
- `azure-openai-responses.ts:243-245`

Anthropic、Google、Bedrock、Mistral adapter **从不调用 `resolveSamplingParams`**，
`samplingParams` 被 `buildBaseOptions` 计算出来后直接丢弃。

### 8.3 各采样参数在 pi 中的处理方式

| 参数 | pi 处理方式 | 命名字段？ | 跨 provider？ |
|---|---|---|---|
| `temperature` | 命名字段，各 adapter 自行写入 + 条件门控 | ✅ | 部分（Anthropic 有门控） |
| `maxTokens` | 命名字段 + compat 选 wire 字段名 | ✅ | ✅（通过 `maxTokensField` compat） |
| `top_p` | 仅 `samplingParams` 逃生舱，OpenAI-compat only | ❌ | ❌ |
| `top_k` | 仅 doc 注释提及，**无任何 adapter 处理** | ❌ | ❌ |
| `min_p` | 仅 doc 注释提及 | ❌ | ❌ |
| `typical_p` | 代码库中完全不存在 | ❌ | ❌ |
| `top_a` | 代码库中完全不存在 | ❌ | ❌ |
| `repetition_penalty` | 仅 doc 注释提及，`samplingParams` 逃生舱 | ❌ | ❌ |
| `frequency_penalty` | 仅 Mistral wire-remap 有（死代码，不填充） | ❌ | ❌ |
| `presence_penalty` | 仅 Mistral wire-remap 有（死代码，不填充） | ❌ | ❌ |
| `seed` | 无命名字段；Mistral 内部 ID 生成用 `seedBase`（非生成参数） | ❌ | ❌ |
| `stop` / `stop_sequences` | **显式不支持**。Anthropic 注释："We don't supply stop sequences" | ❌ | ❌ |
| `response_format` / JSON mode | **非请求级字段**，是 per-tool `constrainedSampling`（`json_schema` strict prefer/require，或 grammar 变体） | ❌ | ❌（tool 级） |
| `parallel_tool_calls` | 仅 Codex 硬编码 `true`；Mistral wire-remap 有但不填充 | ❌ | ❌ |
| `logprobs` | 仅 llama.cpp 分类器内部用，非 chat 选项 | ❌ | ❌ |
| `watermark` | 代码库中不存在 | ❌ | ❌ |

### 8.4 pi 的 compat schema（provider 级 wire 字段选择器）

compat 字段在 `Model.compat` 上，由 `baseUrl` 自动检测或 per-model 覆盖。这是 pi
在"命名字段"层做 provider 适配的核心机制——不是归一化值，而是选择 wire 字段名/格式。

| compat 字段 | 类型 | 作用 | 适用 |
|---|---|---|---|
| `maxTokensField` | `"max_completion_tokens"\|"max_tokens"` | 选择 max_tokens 的 wire 字段名 | OpenAI-compat |
| `thinkingFormat` | 11 种选择器 | 选择思考参数的 wire 格式 | OpenAI-compat |
| `thinkingTokenBudgetField` | `"thinking_token_budget"\|"thinking_budget"\|"thinking_budget_tokens"` | token 预算的 wire 字段名（vLLM/Qwen/llama.cpp） | OpenAI-compat |
| `cacheControlFormat` | `"anthropic"` | Anthropic 式 `cache_control` 标记（OpenRouter 上的 Anthropic 模型） | OpenAI-compat |
| `sessionAffinityFormat` | `"openai"\|"openai-nosession"\|"openrouter"` | session 亲和性 header 格式 | 跨 API |
| `supportsTemperature` | `boolean` | 是否接受 `temperature`（Anthropic Opus 4.7+ 拒绝） | Anthropic |
| `supportsReasoningEffort` | `boolean` | 是否接受 `reasoning_effort` | OpenAI-compat |
| `supportsDeveloperRole` | `boolean` | developer role vs system | 跨 API |
| `supportsUsageInStreaming` | `boolean` (default true) | `stream_options.include_usage` | OpenAI-compat |
| `supportsFinishReason` | `boolean` (default true) | 流式 `finish_reason`（false 时 pi 推断 stop/toolUse） | OpenAI-compat |
| `supportsStrictMode` | `boolean` | tool 定义中 `strict` 字段 | 跨 API |
| `supportsStore` | `boolean` | 是否支持 `store` 字段 | OpenAI-compat |
| `supportsLongCacheRetention` | `boolean` | 长 cache retention（`prompt_cache_retention:"24h"` / Anthropic `cache_control.ttl:"1h"`） | 跨 API |
| `forceAdaptiveThinking` | `boolean` | 强制 `thinking.type: "adaptive"` + `output_config.effort` | Anthropic |
| `supportsMidConvoEffort` | `boolean` | managed-effort 模式（effort-only 系统消息 + thinking binding） | Anthropic |
| `supportsMidConvoSystemMessages` | `boolean` | 对话中途插入 system/dev 消息 | 跨 API |
| `requiresToolResultName` | `boolean` | tool result 需要 `name` 字段 | OpenAI-compat |
| `requiresAssistantAfterToolResult` | `boolean` | tool result 间需 assistant 消息 | OpenAI-compat |
| `requiresThinkingAsText` | `boolean` | thinking → `<thinking>` 文本块 | OpenAI-compat |
| `vllmPriority` | `number` | vLLM 调度器优先级 | vLLM |
| `openRouterRouting` | 对象 | OpenRouter 路由偏好（fallbacks/data_collection/zdr/...） | OpenRouter |

### 8.5 pi 的 per-tool 结构化输出（`constrainedSampling`）

pi 不用请求级 `response_format`，而是在 **Tool 级别** 声明结构化输出约束：

```typescript
// types.ts:749-754
type ConstrainedSamplingConfig =
  | { type: "json_schema"; strict: "prefer" | "require" }
  | { type: "grammar"; variants: Partial<Record<GrammarFormat, string>> };
// GrammarFormat = "openai_lark" | "openai_regex"

interface Tool {
  constrainedSampling?: false | ConstrainedSamplingConfig;
}
```

- `json_schema` + `strict: "prefer"` → 尽量用 strict，不支持则降级
- `json_schema` + `strict: "require"` → 必须 strict，不支持则抛错
- `grammar` + `variants` → per-provider 语法约束（Lark/regex）

共享解析器 `constrained-sampling.ts`：
- `makeStrictJsonSchema` — 把 JSON schema 重写为 strict 子集（去 `$ref`/`allOf`/`oneOf`，强制 `additionalProperties:false`，所有属性 required）
- `resolveJsonSchemaStrictSampling` — 根据 `constrainedSampling` 返回 `true`/`undefined`/throw
- Anthropic 额外有 runtime fallback：400 时 retry 非 strict

### 8.6 Sherry 可借鉴的 pi 适配字段（思考等级之外）

| pi 字段 | Sherry 现状 | 可借鉴方向 | 优先级 |
|---|---|---|---|
| `samplingParams` 逃生舱 | ❌ 无 | 加一个 `extra_body` dict passthrough（已有部分），允许用户传任意 sampling 参数给 OpenAI-compat provider | P2 |
| `samplingParamsByThinkingLevel` | ❌ 无 | per-thinking-level 采样参数覆盖（如 high 时降 temperature） | P3 |
| `maxTokensField` compat | ❌ 硬编码 | 声明式选择 wire 字段名（`max_tokens` vs `max_completion_tokens`） | P2 |
| `cacheRetention` | ❌ 无 | prompt cache 偏好（none/short/long）→ per-provider 映射到 `prompt_cache_key`/`cache_control` | P2 |
| `constrainedSampling` (per-tool) | Sherry 用 `response_format` | 改为 per-tool strict 声明 + Anthropic runtime fallback | P3 |
| `supportsTemperature` compat | ❌ 无条件发 | Anthropic thinking 开启时不发 temperature（Opus 4.7+ 会拒） | P1（随 Anthropic adaptive 一起） |
| `sessionId` → cache key | ❌ 无 | session ID → prompt cache key 映射 | P3 |
| `transport` | ❌ 硬编码 SSE | 流式传输偏好（SSE/WebSocket/auto） | P3 |

### 8.7 结论：pi 不适配重复惩罚参数

**pi 对 `repetition_penalty` / `frequency_penalty` / `presence_penalty` 的态度是"不适配"**——
它们不是命名字段，不做跨 provider 归一化，只在 `samplingParams: Record<string, unknown>`
逃生舱中作为原始 key 透传，且仅 OpenAI-compat adapter 应用。

如果 Sherry 要适配这些参数，有三条路：
1. **学 pi 的逃生舱**：加一个 `sampling_params: dict[str, Any]` 字段，仅透传给 OpenAI-compat
   provider（vLLM/SGLang/llama.cpp 等自部署场景有用，但 Anthropic/Google 用不了）
2. **比 pi 更进一步**：为 `top_p` / `top_k` / `repetition_penalty` 做真正的跨 provider
   命名适配（需要 per-provider 映射表，Anthropic 支持 `top_p`/`top_k`，Google 支持
   `top_p`/`top_k`/`temperature`，OpenAI 支持 `top_p`/`frequency_penalty`/`presence_penalty`）
3. **前端自配**：扩展方案 C 的 `CustomThinkingMapping` 为 `CustomSamplingMapping`，
   让用户在前端填 per-provider 的采样参数 wire 字段名 + 值

推荐路线 1（逃生舱）+ 路线 3（前端自配），不做路线 2（太重，provider 差异太大，维护成本高）。

---

## 9. 关键文件清单

### 后端新增
| 文件 | 用途 |
|---|---|
| `config/features/agent_side/thinking_levels.py` | 7 级枚举 + ThinkingLevelMap TypedDict + ThinkingFormat Enum + ThinkingConfig |

### 后端修改
| 文件 | 改动 |
|---|---|
| `config/features/agent_side/reasoning_budget.py` | 新增 `thinking_budgets: dict[str, int]` |
| `config/features/agent_side/__init__.py` | re-export 新模块 |
| `models/LLMs/reasoning_payload.py` | 重写为声明式分发 + clamp + 11 种 thinkingFormat |
| `models/LLMs/main_llm.py` | `_reasoning_for_state` 改为 level 参数 |
| `agent/middlewares/thinking_control/core.py` | 7 级 cache key + clamp |
| `server/trigger/http/session_settings.py` | value 值域扩展 + GET /thinking/levels + (P3) GET/PUT /thinking/custom |
| `server/service/session_settings_service.py` | apply_thinking_choice 扩展 + (P3) custom mapping |
| `runtime/session/state_keys.py` | (P3) 新增 CUSTOM_THINKING_MAPPING |
| `agent/tools/subagent/spawn/thinking.py` | 适配新 level 枚举 |

### 前端修改
| 文件 | 改动 |
|---|---|
| `client/app/pages/home/components/ThinkingToggle.vue` | 统一 7 级下拉 |
| `client/app/stores/thinking.ts` | ThinkingValue 类型扩展 + availableLevels |
| `client/app/composables/bridge/session.ts` | fetchThinkingLevels + (P3) custom mapping |
| `client/app/pages/home/components/ThinkingCustomMappingPanel.vue` | (P3) 新增 |
| i18n 文件 | 7 级 × 4 语 = 28 个新 key |

### 测试新增
| 文件 | 用途 |
|---|---|
| `tests/models/test_reasoning_payload.py` | 声明式分发 + clamp |
| `tests/agent/middlewares/test_thinking_control.py` | 7 级 cache key |
| `tests/server/test_session_settings.py` | value 值域扩展 |
| `client/app/stores/__tests__/thinking.test.ts` | 统一选择器 |
| `client/app/pages/home/components/__tests__/ThinkingToggle.integration.test.ts` | 7 级渲染 |

---

## 附录 A：pi `thinkingLevelMap` 值语义速查

| 值 | 语义 | 示例 |
|---|---|---|
| `string` | 该 level 支持且发此 wire 值 | `{high: "high"}`, `{off: "none"}` |
| `null` | 该 level 不支持，clamp 跳过 | `{off: null}`（always-think） |
| `undefined`/omitted | 用 adapter 内置默认映射 | 不填 `medium` → adapter 默认 `"medium"` |

`xhigh` / `max` 额外要求显式声明（非 undefined），否则视为不支持。

## 附录 B：pi `clampThinkingLevel` 逻辑

```
1. 如果 level 在 availableLevels 中 → 直接返回
2. 从 level 的索引位置开始：
   a. 先向上搜索（i → i+1 → ... → max）找第一个 available → 返回
   b. 再向下搜索（i-1 → i-2 → ... → 0）找第一个 available → 返回
3. 全都没有 → 返回 availableLevels[0] 或 "off"
```

## 附录 C：pi 11 种 thinkingFormat wire 字段对照

| thinkingFormat | wire 字段 | 值示例 |
|---|---|---|
| `openai` | `reasoning_effort` (top-level) | `low/medium/high` |
| `openrouter` | `reasoning.effort` (nested) | `none + 标准` |
| `deepseek` | `thinking.type` + `reasoning_effort` | `enabled/disabled` + `effort` |
| `together` | `reasoning.enabled` + `reasoning_effort` | `bool + effort` |
| `baseten` | `chat_template_args` + `reasoning_effort` | kwargs + effort |
| `zai` | `thinking.type` (enabled/disabled, + clear_thinking) | `enabled/disabled` |
| `qwen` | `enable_thinking` (bool) + `reasoning_effort` | `bool + effort` |
| `chat-template` | `chat_template_kwargs` ($var placeholders) | kwargs |
| `qwen-chat-template` | `chat_template_kwargs.{enable_thinking, preserve_thinking}` | kwargs |
| `string-thinking` | `thinking` (top-level string) | string |
| `ant-ling` | `reasoning.effort` (only when mapped) | effort |

## 附录 D：pi 全部跨 provider 适配字段速查（非思考等级）

| 字段 | 命名？ | 跨 provider？ | Sherry 可借鉴？ |
|---|---|---|---|
| `temperature` | ✅ | 部分（Anthropic 有门控） | ✅ 学条件门控 |
| `maxTokens` | ✅ | ✅（compat 选字段名） | ✅ 学 compat 选字段名 |
| `toolChoice` | ✅ | 部分（base auto/none） | 已有 |
| `cacheRetention` | ✅ | ✅ | ✅ 学 prompt cache 偏好 |
| `sessionId` | ✅ | ✅ | ✅ 学 cache key 映射 |
| `transport` | ✅ | ✅ | P3 |
| `samplingParams` | 逃生舱 | 仅 OpenAI-compat | ✅ 加自由式 dict 透传 |
| `samplingParamsByThinkingLevel` | 逃生舱 | 仅 OpenAI-compat | P3 |
| `constrainedSampling` (per-tool) | ✅ | ✅ | ✅ 学 per-tool strict |
| `top_p` / `top_k` / `min_p` | ❌ | ❌ | ❌ 不适配（逃生舱 only） |
| `repetition_penalty` | ❌ | ❌ | ❌ 不适配（逃生舱 only） |
| `frequency_penalty` / `presence_penalty` | ❌ | ❌ | ❌ 不适配（逃生舱 only） |
| `seed` | ❌ | ❌ | ❌ 不适配 |
| `stop` / `stop_sequences` | ❌ | ❌ | ❌ 显式不支持 |
| `response_format` | ❌（tool 级） | ❌ | ✅ 学 per-tool strict |
| `logprobs` | ❌ | ❌ | ❌ 仅分类器用 |
| `watermark` | ❌ | ❌ | ❌ 不存在 |

---

## 落地记录（2026-10-15）

**已落地（Phase 1 P0 + Phase 2 P1 主体）**

- `config/features/agent_side/thinking_levels.py` 新建：`ThinkingLevel`（7 级）、
  `THINKING_LEVELS` / `EXTENDED_THINKING_LEVELS`（钳位顺序）、`ThinkingLevelMap`、
  `ThinkingFormat`（11 种 wire 格式枚举）、`ThinkingConfig`（`THINKING_LEVEL_CONFIG`）；
  经 `config/features/agent_side/__init__.py` 与 `config/features/__init__.py` 逐层 re-export。
- `models/LLMs/reasoning_payload.py` 重写为声明式引擎：`_FORMAT_LEVEL_MAPS`（每格式默认映射）
  + `_MODEL_LEVEL_OVERRIDES`（glm-5 / glm-4.5 / glm-4.6 的 per-level 声明）+ 五个新公共函数
  （`resolve_thinking_format` / `resolve_thinking_level_map` / `get_supported_thinking_levels`
  / `clamp_thinking_level` / `build_thinking_kwargs`）。旧入口
  （`build_reasoning_kwargs` / `build_thinking_off_kwargs` / `build_thinking_floor_kwargs` /
  `build_thinking_level_kwargs` / `thinking_control_mode` / `get_thinking_budget`）保留为
  wrapper，现有四类 provider 的线上 payload **逐字节不变**（兼容性由既有测试钉住）。
  Anthropic（budget 形式）与 Google（`thinking_level` 离散形式）走原生分支；
  OpenRouter / Qwen / Together / Baseten / chat-template / string-thinking / ant-ling 走格式分发。
- `models/LLMs/main_llm.py`：`build_main_llm` / `build_main_llm_for_profile` 新增统一
  `level` 别名参数（等价 `thinking_level`）。
- `agent/middlewares/thinking_control/core.py`：档位 token 集合改为从
  `EXTENDED_THINKING_LEVELS` 派生（7 级全部可入 cache key）；`_off_floor_learned`
  阶梯保留为运行时安全网（声明式映射已把已知模型的 off 提前钳位，阶梯只在映射未覆盖的
  网关新行为时兜底）。
- `server/service/session_settings_service.py`：值域 `_VALID_LEVELS` 改为从
  `EXTENDED_THINKING_LEVELS` 派生（7 级）；`GET /sessions/thinking` 的返回体新增
  `levels`（当前模型支持的档位列表，钳位顺序）。
- 前端：`bridge/session.ts` 新增 `ThinkingLevel` 类型与 `levels` 字段；
  `stores/thinking.ts` 新增 `levelsBySession` / `availableLevels()`（后端驱动，兜底全 7 级）；
  `ThinkingToggle.vue` 在模型声明了多个档位时自动切换为**统一档位选择器**（含 off，
  按后端列表过滤），单一 `off` 的模型保持原开关形态；i18n 四语新增
  minimal/medium/xhigh 标签；移动端循环也改为遍历可用档位。
- 测试：`tests/models/LLMs/test_thinking_levels_declarative.py` 新增 30 例（支持列表、
  钳位、各格式 wire、显式覆盖 seam）；`tests/models/LLMs/` 全绿 103 例；
  前端 `thinking.test.ts` / `ThinkingToggle.integration.test.ts` 新增统一选择器与
  7 级回归用例；客户端 unit 798 / integration 349 / typecheck / dpdm 全绿。

**未落地（本计划剩余项）**

- Phase 2 未做的部分：Anthropic adaptive / managed-effort（现为 budget 形式）、
  Bedrock（Claude/GPT）分支、per-level token budget 表（现为族常量）。
- Phase 3 (P2) 全部：`CustomThinkingMapping` + state key、`GET/PUT /sessions/thinking/custom`、
  `ThinkingCustomMappingPanel.vue` 与安全约束。
  （引擎侧的 seam 已就位：`build_thinking_kwargs(thinking_format=..., thinking_level_map=...)`
  两个显式参数即为该功能的接入点。）
