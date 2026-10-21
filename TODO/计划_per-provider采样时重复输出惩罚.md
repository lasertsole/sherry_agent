# 计划：per-provider 采样时重复输出惩罚（对齐 PI agent）

- 建档日期：2026-10-21
- 状态：方案设计，未开工
- 来源：跨项目调研（PI agent `samplingParams` + openclaw per-transport penalty + sherry `provider_max_output.py` 模板）
- 严重性：中（sherry 只有后置 `OutputRepetitionGuard` 检测，没有采样时 logit 惩罚——LLM 可能持续产出重复内容直到 HALT）
- 借鉴：PI agent 的 `samplingParams`（`pi/packages/ai/src/types.ts:226-232`）+ openclaw 的 per-transport penalty 过滤（`openclaw/packages/ai/src/transports/openai-completions-params.ts:355-359`）+ sherry `provider_max_output.py` 的 provider→kwarg 映射模式

---

## 0. 问题

### 0.1 sherry 当前只有后置检测，没有采样时惩罚

| | 采样时惩罚（logit 级） | 后置检测（输出级） |
|---|---|---|
| **sherry 有？** | ✗ 无 | ✓ `OutputRepetitionGuard`（`agent/middlewares/output_repetition_guard/core.py:85`） |
| 机制 | `frequency_penalty` / `presence_penalty` 调 token 概率，惩罚已出现 token | model 产出后检查 hash + regex，WARN/HALT |
| 效果 | **预防**——重复 token 概率降低，输出自然不重复 | **反应**——重复已发生后 nudge 或 stop |
| 配置 | 模型参数（用户设 `frequency_penalty=0.5`） | 检测阈值（`max_identical_outputs=3` 等） |

sherry 的 `OutputRepetitionGuard` 是**反应式**的——等重复发生了再 WARN/HALT。**没有预防式**的采样时惩罚。LLM 可能持续产出重复内容直到第 3 次 HALT——浪费 token。

### 0.2 sherry 的 `init_chat_model` 已支持传任意 kwargs，但缺 provider 映射

`init_chat_model(**config)` 把 config dict 的所有 kwargs 转发给 provider 的 langchain ChatModel 类。如果 config 里有 `frequency_penalty=0.5`，OpenAI 的 `ChatOpenAI` 接受它，但 Anthropic 的 `ChatAnthropic` **不接受**（Anthropic API 不支持 penalty 参数）。

需要一个**provider → kwarg 映射**，跟 `provider_max_output.py` 完全同模式：
- `provider_max_output.py` 把 `max_tokens` 映射到 `max_output_tokens`（Google）或 `max_completion_tokens`（OpenAI o-series）
- 新的 `provider_repetition_penalty.py` 把 `frequency_penalty`/`presence_penalty` 映射到 provider 支持的 kwarg 名，不支持的 provider 返回 `{}`（M4 skip）

### 0.3 跨项目对照

| 项目 | 采样时重复惩罚 | 机制 |
|---|---|---|
| **PI agent** | ✓ | `samplingParams: Record<string, unknown>`（`types.ts:133-134`）——泛型 dict，OpenAI-compatible adapter 原样转发，native API（Anthropic/Google）忽略。per-model + per-thinking-level override（`resolveSamplingParams`） |
| **openclaw** | ✓ | typed `frequencyPenalty`/`presencePenalty` on shared options——只 OpenAI Completions transport 转发（Responses/Anthropic transport 跳过）。Ollama 用 `repeat_penalty` |
| **hermes** | ✗ | 只有 `ToolCallSignature` 循环检测（后置） |
| **codex** | ✗ | 无 |
| **claudecode** | ✗ | 无 |
| **sherry** | ✗ | 只有 `OutputRepetitionGuard`（后置） |

### 0.4 各 provider 支持的 penalty kwargs

| Provider | 支持的 penalty kwargs | 范围 |
|---|---|---|
| OpenAI / deepseek / zhipu / moonshot / dashscope / siliconflow / mistral / cohere / xai / fireworks 等（OpenAI-compatible） | `frequency_penalty` + `presence_penalty` | -2.0 ~ 2.0 |
| Google (Gemini/Vertex) | `frequency_penalty` + `presence_penalty`（langchain 映射到 `generation_config`） | -2.0 ~ 2.0 |
| Anthropic (Claude) | **不支持**——Anthropic Messages API 无 penalty 参数 | N/A |
| Ollama (local) | `repeat_penalty`（native）+ 也接受 `frequency_penalty`/`presence_penalty` | 0.0 ~ 2.0（`repeat_penalty`） |
| Bedrock | model-dependent——Claude via Bedrock 不支持；其他模型可能支持 | N/A |

---

## 1. 设计

### 1.1 `provider_repetition_penalty.py`——provider → kwarg 映射

**新文件**：`models/LLMs/provider_repetition_penalty.py`

镜像 `provider_max_output.py`（`:35-135`）的精确模式：

```python
"""Per-provider repetition penalty kwarg mapping.

Mirrors provider_max_output.py's contract:
- A provider absent from the table is UNSUPPORTED: no field passed (M4 skip).
- The configured value is only applied when set (if-set-then-pass, M3).
- clean_client_kwargs drops None/empty, so an unset penalty is a safe no-op.

Borrowed from PI agent's samplingParams (pi/packages/ai/src/types.ts:226-232)
and openclaw's per-transport penalty filtering
(openclaw/packages/ai/src/transports/openai-completions-params.ts:355-359).
"""

# Provider → supported penalty kwargs
# OpenAI-compatible providers accept both frequency_penalty and presence_penalty
# Google accepts both (langchain maps to generation_config)
# Anthropic does NOT support penalty params — absent from table (M4 skip)
# Ollama uses repeat_penalty (native) + also accepts frequency/presence
_OPENAI_COMPATIBLE_PROVIDERS = frozenset({
    "openai", "openrouter", "deepseek", "zhipu", "zhipuai",
    "moonshot", "dashscope", "qwen", "siliconflow",
    "mistral", "mistralai", "cohere", "xai",
    "fireworks", "perplexity", "upstage",
    "azure_openai", "azure_ai",
    "groq", "volcengine", "minimax", "together",
    "baseten", "aihubmix",
    "openai_responses", "azure_responses",
    # OpenAI-compatible local
    "vllm",
})

_GOOGLE_PROVIDERS = frozenset({
    "google", "google_genai", "google-genai",
    "google_vertexai", "vertexai", "gemini",
})

_OLLAMA_PROVIDERS = frozenset({
    "ollama",
})

# Anthropic / Bedrock-anthropic: absent from all tables → M4 skip (returns {})


def resolve_repetition_penalty_kwargs(
    provider: str,
    frequency_penalty: float | None,
    presence_penalty: float | None,
    *,
    repeat_penalty: float | None = None,  # Ollama native
) -> dict[str, float]:
    """Return a {kwarg: value} dict for the provider's supported penalty params.

    Mirrors resolve_max_output_kwargs (provider_max_output.py:76-114):
    - Provider not in any table → return {} (M4 skip)
    - Value is None → don't include that kwarg (M3 if-set-then-pass)
    - Value is set → include {kwarg: value}
    """
    p = provider.lower().strip()
    kwargs: dict[str, float] = {}

    if p in _OPENAI_COMPATIBLE_PROVIDERS or p in _GOOGLE_PROVIDERS:
        if frequency_penalty is not None:
            kwargs["frequency_penalty"] = float(frequency_penalty)
        if presence_penalty is not None:
            kwargs["presence_penalty"] = float(presence_penalty)
    elif p in _OLLAMA_PROVIDERS:
        # Ollama: repeat_penalty is native; frequency/presence also accepted
        if repeat_penalty is not None:
            kwargs["repeat_penalty"] = float(repeat_penalty)
        if frequency_penalty is not None:
            kwargs["frequency_penalty"] = float(frequency_penalty)
        if presence_penalty is not None:
            kwargs["presence_penalty"] = float(presence_penalty)
    # Anthropic / Bedrock-anthropic / unknown: return {} (M4 skip)

    return kwargs
```

### 1.2 配置

**新文件**：`config/features/agent_side/repetition_penalty.py`

```python
"""Per-provider sampling-time repetition penalty config (aligned with PI agent)."""

from typing import TypedDict


class RepetitionPenaltyConfig(TypedDict):
    #: OpenAI-compatible + Google: frequency_penalty (-2.0~2.0, 0=disabled)
    #: None = 不传该参数（M3 if-set-then-pass）
    frequency_penalty: float | None
    #: OpenAI-compatible + Google: presence_penalty (-2.0~2.0, 0=disabled)
    presence_penalty: float | None
    #: Ollama native: repeat_penalty (0.0~2.0, 1.0=disabled)
    repeat_penalty: float | None


REPETITION_PENALTY: RepetitionPenaltyConfig = {
    "frequency_penalty": 0.3,   # 轻微惩罚已出现 token——降低重复概率但不阻断创造
    "presence_penalty": 0.2,    # 轻微鼓励新 token——降低"重复同一句话"的概率
    "repeat_penalty": 1.1,      # Ollama: 1.1 = 轻微惩罚（1.0=disabled, 1.3=强惩罚）
}
```

**默认值理由**：
- `frequency_penalty=0.3` + `presence_penalty=0.2`——轻微惩罚（PI agent 和 openclaw 的典型值在 0.1~0.5 之间）。太低无效，太高（>1.0）会压制模型创造性导致输出不连贯。
- `repeat_penalty=1.1`——Ollama 的典型值（llama.cpp 默认 1.1）。
- 用户可在 `.models` 配置里调高（如 `frequency_penalty=0.8` 对付强重复倾向的模型）。
- `None` = 不传——用户可以设 `null` 关闭某个 penalty。

### 1.3 合并到 `init_chat_model` 调用

**文件**：`models/LLMs/main_llm.py`（主路径）、`models/LLMs/family_llm.py`（taskflow/compression）、`models/LLMs/reasoner_llm.py`（reasoner）

在 `resolve_max_output_kwargs` 旁边加 `resolve_repetition_penalty_kwargs`：

```python
# main_llm.py:93-112（现有 model_config 构建）
model_config = ModelEnvBuilder(...).build({
    "temperature": 0,
    "max_retries": ...,
    "timeout": ...,
    "stream_chunk_timeout": ...,
    "profile": {"max_input_tokens": ...},
    **resolve_max_output_kwargs(model_provider, api_name, MAX_OUTPUT_TOKENS),  # 现有
    # ★ 新增：per-provider 重复惩罚
    **resolve_repetition_penalty_kwargs(
        model_provider,
        REPETITION_PENALTY["frequency_penalty"],
        REPETITION_PENALTY["presence_penalty"],
        repeat_penalty=REPETITION_PENALTY["repeat_penalty"],
    ),
})
```

`family_llm.py` 和 `reasoner_llm.py` 同样加。

**`clean_client_kwargs`**（`env_builder.py:34`）已经 drop None/empty——`resolve_repetition_penalty_kwargs` 返回 `{}` 时是 no-op，不影响 provider 不支持的模型。

### 1.4 跟 `OutputRepetitionGuard` 的关系

两层互补，不替代：

| | 采样时惩罚（本计划） | 后置检测（现有 `OutputRepetitionGuard`） |
|---|---|---|
| 时机 | model 生成时（logit 级） | model 产出后（输出级） |
| 机制 | 降低已出现 token 的概率 | hash + regex 检测重复 → WARN/HALT |
| 效果 | **预防**——减少重复发生的概率 | **反应**——重复发生后 nudge/stop |
| 配置 | `REPETITION_PENALTY` dict（provider kwargs） | `REPETITION_GUARD` dict（检测阈值） |
| 保留？ | 新增 | **保留**——惩罚不能 100% 消除重复（LLM 可能仍然重复），后置检测兜底 |

**两层同时跑**：采样时惩罚降低重复概率 → 如果仍然重复（惩罚不够强 / 模型特别顽固）→ `OutputRepetitionGuard` 检测到 → WARN/HALT。belt and suspenders。

### 1.5 per-session / per-thinking-level override（借鉴 PI `samplingParamsByThinkingLevel`）

PI agent 支持 per-thinking-level 的 sampling 参数覆盖（`pi/packages/ai/src/api/simple-options.ts:24-34`）。sherry 可以在未来的 `.models` 配置里加 per-model override：

```jsonc
// .models（未来扩展，不在本计划范围内）
"MAIN_LLM_FREQUENCY_PENALTY": 0.3,
"MAIN_LLM_PRESENCE_PENALTY": 0.2,
// 或 per-thinking-level（跟 reasoning_payload.py 的 thinkingLevelMap 同模式）
"MAIN_LLM_PENALTY_BY_THINKING_LEVEL": {
  "high": {"frequency_penalty": 0.1, "presence_penalty": 0.1},  // 高 thinking 时降惩罚（保留推理深度）
  "off": {"frequency_penalty": 0.3, "presence_penalty": 0.2},   // 默认
}
```

**本计划只做全局默认值**（`REPETITION_PENALTY` dict）——per-model / per-thinking-level override 是 P2 扩展。

---

## 2. 关键文件清单

| 文件 | 改动 |
|---|---|
| `models/LLMs/provider_repetition_penalty.py`（新） | provider → kwarg 映射 + `resolve_repetition_penalty_kwargs` |
| `config/features/agent_side/repetition_penalty.py`（新） | `REPETITION_PENALTY` config dict |
| `config/features/__init__.py` | 导出 `REPETITION_PENALTY` |
| `models/LLMs/main_llm.py` | `model_config` 构建里加 `**resolve_repetition_penalty_kwargs(...)`（`:110` 附近） |
| `models/LLMs/family_llm.py` | 同（`:98-102` 附近） |
| `models/LLMs/reasoner_llm.py` | 同（`:24-40` 附近——当前没调 `resolve_max_output_kwargs`，也要加 penalty） |

### 复用的已有组件

| 组件 | 文件 | 复用方式 |
|---|---|---|
| `provider_max_output.py` 映射模式 | `models/LLMs/provider_max_output.py:35-135` | `provider_repetition_penalty.py` 镜像同模式 |
| `clean_client_kwargs` drop None/empty | `models/env_builder.py:34` | penalty 返回 `{}` 时自动 no-op |
| `init_chat_model(**config)` kwargs 转发 | `main_llm.py:263` | penalty kwargs 自动转发给 provider ChatModel |
| `REPETITION_GUARD` config 导出模式 | `config/features/__init__.py:57` | `REPETITION_PENALTY` 同模式导出 |

### 不引入的依赖

- 不引入 per-thinking-level override（P2 扩展，本计划只做全局默认）
- 不引入 per-model override（P2 扩展，通过 `.models` 配置）
- 不改 `OutputRepetitionGuard`（后置检测保留，两层互补）
- 不引入 result cache（只调 sampling 参数，不缓存结果）

---

## 3. 测试

| 测试 | 场景 |
|---|---|
| `test_openai_provider_gets_both_penalties` | provider="openai" → 返回 `{frequency_penalty: 0.3, presence_penalty: 0.2}` |
| `test_google_provider_gets_both_penalties` | provider="google" → 返回 `{frequency_penalty: 0.3, presence_penalty: 0.2}` |
| `test_anthropic_provider_skipped` | provider="anthropic" → 返回 `{}`（M4 skip） |
| `test_bedrock_anthropic_skipped` | provider="anthropic_bedrock" → 返回 `{}` |
| `test_ollama_provider_gets_repeat_penalty` | provider="ollama" → 返回 `{repeat_penalty: 1.1, frequency_penalty: 0.3, presence_penalty: 0.2}` |
| `test_unknown_provider_skipped` | provider="custom" → 返回 `{}` |
| `test_none_values_skipped` | `frequency_penalty=None` → 不包含该 kwarg（M3 if-set-then-pass） |
| `test_main_llm_config_includes_penalties` | `main_llm.py` 构建的 config dict 含 `frequency_penalty`（OpenAI provider） |
| `test_main_llm_config_excludes_penalties_for_anthropic` | Anthropic provider 的 config dict 不含 penalty kwargs |
| `test_clean_client_kwargs_drops_empty_penalty` | `resolve_repetition_penalty_kwargs` 返回 `{}` → `clean_client_kwargs` 不加任何 key |
| `test_family_llm_includes_penalties` | `family_llm.py` 构建的 config 含 penalty（TaskFlow/compression 模型也有惩罚） |
| `test_penalty_values_pass_to_init_chat_model` | mock `init_chat_model`，验证 OpenAI provider 的调用 kwargs 含 `frequency_penalty=0.3` |

---

## 4. 优先级与分阶段落地

**P0（全局默认 penalty + provider 映射）**：
- §1.1 `provider_repetition_penalty.py`
- §1.2 `repetition_penalty.py` config
- §1.3 合并到 `main_llm.py` + `family_llm.py`

**P1（覆盖 reasoner + 验证）**：
- §1.3 `reasoner_llm.py` 也加
- 测试覆盖所有 provider 路径

**P2（per-model / per-thinking-level override）**：
- `.models` 配置加 `*_FREQUENCY_PENALTY` / `*_PRESENCE_PENALTY` env 键
- per-thinking-level override（借鉴 PI `samplingParamsByThinkingLevel`）

---

## 5. 回归风险与缓解

| 风险 | 缓解 |
|---|---|
| 惩罚值太高导致输出不连贯 | 默认值轻微（`frequency_penalty=0.3` / `presence_penalty=0.2`）——PI/openclaw 典型值在 0.1~0.5；用户可调低或设 `null` 关闭 |
| 某些 provider 的 strict gateway 拒绝 penalty kwargs | M4 skip——不支持的 provider 返回 `{}`，不传 penalty kwargs；`clean_client_kwargs` 自动 drop |
| Anthropic 模型不支持但 langchain `ChatAnthropic` 静默接受？ | `ChatAnthropic` 不接受 `frequency_penalty`——如果传了会 TypeError 或静默忽略。provider 映射保证不传 |
| 跟 `OutputRepetitionGuard` 冲突？ | 不冲突——惩罚降低重复概率，后置检测兜底。两层独立，不互相依赖 |
| penalty 影响模型推理质量（reasoning 模型特别敏感） | `reasoner_llm.py` 可以设更低 penalty 或 `None`（本计划 P1 加 reasoner 时可设 `frequency_penalty=0.1`） |
| Ollama `repeat_penalty` 跟 OpenAI 的 `frequency_penalty` 语义不同 | 分别映射——Ollama 用 `repeat_penalty`（native）+ 也接受 `frequency_penalty`；两个都传，Ollama 按 native 优先 |

---

## 6. 已知不在本计划范围内

- **per-model override**：通过 `.models` 配置——P2 扩展。本计划只做全局默认值（`REPETITION_PENALTY` dict）。
- **per-thinking-level override**：借鉴 PI `samplingParamsByThinkingLevel`——P2 扩展。高 thinking 时可能需要降惩罚（保留推理深度）。
- **动态调参**：`OutputRepetitionGuard` 检测到重复后自动调高 penalty——P2 增强。本计划是静态默认值。
- **`OutputRepetitionGuard` 改造**：不动——后置检测保留，两层互补。

---

## 7. 一句话

镜像 `provider_max_output.py` 的 provider→kwarg 映射模式，新建 `provider_repetition_penalty.py`——OpenAI-compatible + Google provider 传 `frequency_penalty` + `presence_penalty`，Ollama 加 `repeat_penalty`，Anthropic/Bedrock 返回 `{}`（M4 skip）。合并到 `main_llm.py` / `family_llm.py` / `reasoner_llm.py` 的 `init_chat_model` config 里（跟 `resolve_max_output_kwargs` 同位置）。默认值轻微（`frequency_penalty=0.3` / `presence_penalty=0.2`），借鉴 PI agent `samplingParams` + openclaw per-transport penalty filtering。跟现有 `OutputRepetitionGuard` 后置检测互补——惩罚预防 + 检测兜底。不引入 per-model/per-thinking-level override（P2 扩展）。
