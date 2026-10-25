"""Per-provider max-output-token field selection (borrowed from PI's compat schema).

Sherry builds chat clients through ``init_chat_model``, whose per-provider chat
classes accept different keyword names for the same concept:

* OpenAI-compatible chat models (OpenAI, DeepSeek, Zhipu, Mistral, xAI, ...),
  Azure OpenAI and Anthropic: ``max_tokens``.
* Google Generative AI / Vertex: ``max_output_tokens``.
* The OpenAI **Responses** API: ``max_output_tokens`` (the plan's direct-API
  naming; kept for a future responses-API path).

A provider absent from the table is treated as UNSUPPORTED: the field is not
passed and the provider keeps its own default (M4 — e.g. a Codex-protocol
gateway that rejects unknown parameters). The configured value is only applied
when set (``if-set-then-pass``); an unset value leaves today's behaviour intact.

Two floors apply (M9):
* the business floor ``MIN_MAX_OUTPUT_TOKEN`` (8192) — below it the value is
  refused (boot/import/build), because thinking-mode reasoning and the visible
  answer share this budget;
* the API floor ``OPENAI_RESPONSES_MIN_OUTPUT_TOKENS`` (16), applied with
  ``max()`` only for the Responses-family providers.
"""

from __future__ import annotations

import os

from config.features.agent_side.token_guard import MIN_MAX_OUTPUT_TOKEN

#: API hard floor for the OpenAI Responses family (borrowed from PI).
OPENAI_RESPONSES_MIN_OUTPUT_TOKENS = 16

#: ``init_chat_model`` provider id (or a sherry alias) → accepted kwarg name.
_MAX_OUTPUT_FIELD_BY_PROVIDER: dict[str, str] = {
    # OpenAI-compatible chat models.
    "openai": "max_tokens",
    "openrouter": "max_tokens",
    "deepseek": "max_tokens",
    "zhipu": "max_tokens",
    "zhipuai": "max_tokens",
    "moonshot": "max_tokens",
    "dashscope": "max_tokens",
    "qwen": "max_tokens",
    "siliconflow": "max_tokens",
    "mistral": "max_tokens",
    "mistralai": "max_tokens",
    "cohere": "max_tokens",
    "xai": "max_tokens",
    "fireworks": "max_tokens",
    "perplexity": "max_tokens",
    "upstage": "max_tokens",
    # Azure OpenAI / Anthropic / Bedrock.
    "azure_openai": "max_tokens",
    "azure_ai": "max_tokens",
    "anthropic": "max_tokens",
    "anthropic_bedrock": "max_tokens",
    "bedrock": "max_tokens",
    "bedrock_converse": "max_tokens",
    # Google Generative AI / Vertex (sherry accepts every alias the reasoning
    # payload layer does).
    "google": "max_output_tokens",
    "google_genai": "max_output_tokens",
    "google-genai": "max_output_tokens",
    "google_vertexai": "max_output_tokens",
    "vertexai": "max_output_tokens",
    "gemini": "max_output_tokens",
    # OpenAI Responses family (future direct-API path).
    "openai_responses": "max_output_tokens",
    "azure_responses": "max_output_tokens",
}

_RESPONSES_PROVIDERS = frozenset({"openai_responses", "azure_responses"})


def resolve_max_output_kwargs(
    provider: str | None,
    virtual_model: str | None,
    value: int | None,
) -> dict[str, int]:
    """Return the provider's max-output kwarg, or ``{}`` when unset/unsupported.

    ``virtual_model`` is accepted for API parity with PI's
    ``resolveMaxOutputField(provider, model)`` and future per-model overrides;
    the current table is provider-scoped only.

    Raises:
        ValueError: the configured value is below the 8192 business floor.
    """
    if value is None or value <= 0:
        return {}
    if value < MIN_MAX_OUTPUT_TOKEN:
        raise ValueError(
            f"max output tokens = {value} is below the minimum {MIN_MAX_OUTPUT_TOKEN} "
            "(8K); thinking-mode reasoning and the answer share this budget, so a "
            "smaller value truncates the reply"
        )
    normalized = (provider or "").strip().lower()
    field = _MAX_OUTPUT_FIELD_BY_PROVIDER.get(normalized)
    if field is None:
        return {}
    if normalized == "openai" and virtual_model:
        # OpenAI's o-series / gpt-5 models take ``max_completion_tokens`` (the
        # legacy ``max_tokens`` is rejected). Mirrors PI's maxTokensField.
        try:
            from models.LLMs.reasoning_payload import is_openai_reasoning_model

            if is_openai_reasoning_model(virtual_model):
                field = "max_completion_tokens"
        except Exception:  # noqa: S110 - detection is an optimisation, not a gate
            pass
    if normalized in _RESPONSES_PROVIDERS:
        value = max(int(value), OPENAI_RESPONSES_MIN_OUTPUT_TOKENS)
    return {field: int(value)}


def configured_max_output(env_key: str) -> int | None:
    """Read a ``*_MAX_OUTPUT_TOKEN`` env value, validating the 8K floor.

    An unset/blank value returns ``None`` (M3: no field is passed and the old
    behaviour is preserved). A non-integer value raises with the key named.
    """
    raw = os.getenv(env_key, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{env_key} must be an integer number of tokens, got {raw!r}") from exc
    if value < MIN_MAX_OUTPUT_TOKEN:
        raise ValueError(
            f"{env_key}={value} too small (min {MIN_MAX_OUTPUT_TOKEN}/8K, "
            "thinking mode reasoning + answer share the budget; smaller truncates)"
        )
    return value


__all__ = [
    "MIN_MAX_OUTPUT_TOKEN",
    "OPENAI_RESPONSES_MIN_OUTPUT_TOKENS",
    "configured_max_output",
    "resolve_max_output_kwargs",
]
