"""Provider registry for LLM providers.

Declarative metadata for every provider exposed by
``config.schema.ProvidersConfig``. The registry is consumed exclusively by
``config.schema.Config._match_provider`` and ``get_api_base`` to:

* route a model name to a provider config (by explicit prefix or keyword),
* apply local fallback routing (e.g. plain "llama3.2" on Ollama),
* refuse OAuth providers as implicit fallbacks,
* apply a default ``api_base`` for gateway / local providers.

Each entry is a light spec object with the following attributes:

``name``
    Must equal the matching ``ProvidersConfig`` field name so the config
    can be looked up via ``getattr(config.providers, spec.name, None)``.
``keywords``
    Lower-cased substrings used to tag a model string to this provider.
    Both ``model_lower`` and its ``-``→``_`` normalized form are matched.
``is_oauth``
    Provider requires explicit interactive OAuth selection and must never be
    used as an implicit gateway/local fallback.
``is_local``
    Provider serves local models (e.g. Ollama), enabling keyword-free
    local fallback routing.
``detect_by_base_keyword``
    A distinguishing substring of this provider's ``api_base`` used to
    prioritize a configured local provider over registry-ordered fallbacks.
``is_gateway``
    An API-gateway aggregator; gateways (and local providers) are eligible
    for a default ``api_base`` when none is configured.
``default_api_base``
    Fallback base URL applied by ``Config.get_api_base`` for gateway / local
    providers when the user left ``api_base`` empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class ProviderSpec:
    """Immutable metadata for a single provider."""

    name: str
    keywords: List[str]
    is_oauth: bool = False
    is_local: bool = False
    detect_by_base_keyword: Optional[str] = None
    is_gateway: bool = False
    default_api_base: Optional[str] = None


# Registry order matters: ``fallthrough_order`` and gateway/local fallbacks
# follow the order of ``PROVIDERS``. Explicit prefix matching precedes keyword
# matching (see ``_match_provider``), so ``github-copilot/model`` correctly
# resolves to ``github_copilot`` instead of ``openai_codex``.
PROVIDERS: List[ProviderSpec] = [
    ProviderSpec(
        name="custom",
        keywords=["custom"],
        default_api_base=None,
    ),
    ProviderSpec(
        name="azure_openai",
        keywords=["azure"],
    ),
    ProviderSpec(
        name="anthropic",
        keywords=["anthropic", "claude"],
    ),
    ProviderSpec(
        name="openai",
        keywords=["openai", "gpt", "chatgpt", "o1", "o3"],
    ),
    ProviderSpec(
        name="openrouter",
        keywords=["openrouter"],
        is_gateway=True,
        default_api_base="https://openrouter.ai/api/v1",
    ),
    ProviderSpec(
        name="deepseek",
        keywords=["deepseek"],
    ),
    ProviderSpec(
        name="groq",
        keywords=["groq", "llama"],
    ),
    ProviderSpec(
        name="zhipu",
        keywords=["zhipu", "glm", "bigmodel", "智谱"],
    ),
    ProviderSpec(
        name="dashscope",
        keywords=["dashscope", "qwen", "通义"],
    ),
    ProviderSpec(
        name="vllm",
        keywords=["vllm"],
        is_local=True,
    ),
    ProviderSpec(
        name="ollama",
        keywords=["ollama", "llama", "qwen", "mistral", "deepseek-r1"],
        is_local=True,
        detect_by_base_keyword="11434",
    ),
    ProviderSpec(
        name="gemini",
        keywords=["gemini"],
    ),
    ProviderSpec(
        name="moonshot",
        keywords=["moonshot", "kimi"],
    ),
    ProviderSpec(
        name="minimax",
        keywords=["minimax"],
    ),
    ProviderSpec(
        name="aihubmix",
        keywords=["aihubmix", "aihub"],
        is_gateway=True,
        default_api_base="https://aihubmix.com/v1",
    ),
    ProviderSpec(
        name="siliconflow",
        keywords=["siliconflow", "硅基"],
        is_gateway=True,
        default_api_base="https://api.siliconflow.cn/v1",
    ),
    ProviderSpec(
        name="volcengine",
        keywords=["volcengine", "火山", "doubao"],
    ),
    ProviderSpec(
        name="volcengine_coding_plan",
        keywords=["volcengine_coding", "coding_plan"],
    ),
    ProviderSpec(
        name="byteplus",
        keywords=["byteplus"],
    ),
    ProviderSpec(
        name="byteplus_coding_plan",
        keywords=["byteplus_coding", "coding_plan"],
    ),
    ProviderSpec(
        name="openai_codex",
        keywords=["codex"],
        is_oauth=True,
    ),
    ProviderSpec(
        name="github_copilot",
        keywords=["github", "copilot"],
        is_oauth=True,
    ),
]


_LOOKUP: dict[str, ProviderSpec] = {spec.name: spec for spec in PROVIDERS}


def find_by_name(name: str) -> Optional[ProviderSpec]:
    """Return the spec whose ``name`` equals *name*, or ``None``."""
    return _LOOKUP.get(name)


__all__ = ["ProviderSpec", "PROVIDERS", "find_by_name"]
