"""``build_reasoning_kwargs`` — build provider-correct reasoning kwargs.

``MAIN_LLM_ENABLE_THINKING`` is a *universal* reasoning/thinking switch. Each LLM
provider exposes its reasoning mode under a different, often proprietary payload
format:

* DeepSeek (V3.2+ chat API) routes thinking through ``extra_body``
  ``{"thinking": {"type": "enabled"}}`` — ``ChatDeepSeek`` threads this verbatim
  into the request body.
* The OpenAI family (``ChatOpenAI`` and OpenAI-compatible gateways) exposes
  ``reasoning_effort`` (``"low"`` / ``"medium"`` / ``"high"``) as a first-class
  kwarg, but *only* on o-series / gpt-5 reasoning models.
* Anthropic (``ChatAnthropic``) exposes ``thinking`` + ``budget_tokens`` as a
  first-class kwarg, but *only* on claude-3-7 / claude-4 / opus / sonnet
  reasoning models.

Why it exists
    Historically ``main_llm.py`` hardcoded the DeepSeek ``extra_body`` payload
    behind the switch. That works only for ``model_provider=deepseek``; for any
    other provider the payload is either silently ignored (switch dead) or, when
    passed to a strict OpenAI-compatible endpoint, rejected with ``400`` —
    crashing the main LLM with ``MAIN_LLM_ENABLE_THINKING=true``.

    This module decouples the switch from any single provider: for each provider
    it emits the correct reasoning payload, and *maps the switch to a no-op* for
    providers/models that do not accept one. So flipping the universal switch is
    always safe — it can never inject an unsupported param and never raises.
"""

from __future__ import annotations

from typing import Any

# OpenAI-compatible gateways that accept ``reasoning_effort`` on their reasoning
# models (o-series / gpt-5). Prune a name here if a gateway rejects the param
# even for those models.
_OPENAI_COMPATIBLE = {
    "openai",
    "openrouter",
    "aihubmix",
    "siliconflow",
    "vllm",
    "moonshot",
    "groq",
    "dashscope",
    "zhipu",
    "volcengine",
    "minimax",
}

# Anthropic model prefixes that accept the ``thinking`` param.
_ANTHROPIC_REASONING_PREFIXES = ("claude-3-7", "claude-4", "claude-opus", "claude-sonnet")

# Reasoning token budget for Anthropic's ``thinking`` param. Must stay well under
# the model's ``max_tokens`` or the API rejects the request.
_DEFAULT_ANTHROPIC_BUDGET = 2000

_VALID_REASONING_EFFORTS = ("low", "medium", "high")


def is_openai_reasoning_model(model_name: str) -> bool:
    """Return True iff ``model_name`` accepts the ``reasoning_effort`` param.

    Mirrors the detection logic used by LightRAG's vendored
    ``is_openai_reasoning_model`` (o-series / gpt-5 series). Any ``org/model``
    prefix is stripped so router-style names (e.g. ``openai/o3-mini``,
    ``anthropic/gpt-5``) still match. Duplicated here rather than imported from
    the vendored skill path to avoid coupling to an independently-updatable
    vendored tree.
    """
    name = model_name.lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name.startswith(("o1", "o3", "o4", "gpt-5"))


def _is_anthropic_reasoning_model(model_name: str) -> bool:
    """Return True iff ``model_name`` accepts the ``thinking`` param."""
    name = model_name.lower()
    return name.startswith(_ANTHROPIC_REASONING_PREFIXES)


def build_reasoning_kwargs(
    provider: str | None,
    model_name: str | None,
    enabled: bool,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Return kwargs to merge into ``init_chat_model(...)`` for reasoning mode.

    Returns ``{}`` (never ``None``) when the switch is off or the provider/model
    does not accept a reasoning payload. Never raises and never injects an
    unsupported parameter, so enabling the universal switch is always crash-safe.

    Parameters
    ----------
    provider:
        The ``model_provider`` value used to build the model (e.g. ``deepseek``,
        ``openai``, ``anthropic``, ``openrouter``, ``ollama``).
    model_name:
        The concrete model name (e.g. ``deepseek-chat``, ``o3-mini``,
        ``claude-opus-4-5``). Used to gate payloads that only reasoning models
        accept.
    enabled:
        The universal ``MAIN_LLM_ENABLE_THINKING`` switch value.
    reasoning_effort:
        Optional ``low`` / ``medium`` / ``high`` mapping to the OpenAI family's
        ``reasoning_effort``. Defaults to ``"high"`` when unset.
    """
    if not enabled or not provider:
        return {}

    provider = provider.strip().lower()

    # DeepSeek V3.2+ chat API: thinking carried via ``extra_body`` so
    # ``ChatDeepSeek`` threads it directly into the request body.
    if provider == "deepseek":
        return {"extra_body": {"thinking": {"type": "enabled"}}}

    # OpenAI family (+ compatible gateways): ``reasoning_effort`` is a
    # first-class ``ChatOpenAI`` kwarg. Inject ONLY for reasoning models; other
    # models reject the param with a 400.
    if provider in _OPENAI_COMPATIBLE:
        if model_name and is_openai_reasoning_model(model_name):
            effort = (reasoning_effort or "high").strip().lower()
            if effort in _VALID_REASONING_EFFORTS:
                return {"reasoning_effort": effort}
        return {}

    # Anthropic: ``thinking`` is a first-class ``ChatAnthropic`` kwarg. Inject
    # ONLY for reasoning models; other Claude models reject the param with a 400.
    if provider == "anthropic":
        if model_name and _is_anthropic_reasoning_model(model_name):
            return {
                "thinking": {
                    "type": "enabled",
                    "budget_tokens": _DEFAULT_ANTHROPIC_BUDGET,
                }
            }
        return {}

    # gemini, ollama, azure_openai, custom, codex, copilot, byteplus, etc. →
    # documented no-op: the switch stays safe but reasoning payloads are not
    # (yet) mapped for these providers.
    return {}


__all__ = ["build_reasoning_kwargs", "is_openai_reasoning_model"]
