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
* Zhipu GLM (glm-4.5+ / glm-4.6 / glm-5 series, incl. ``glm-*-flash`` variants)
  served through OpenAI-compatible gateways (e.g. ``open.bigmodel.cn/api/paas/v4``)
  exposes thinking via the DeepSeek-style request-body key
  ``{"thinking": {"type": "enabled"}}`` and streams chain-of-thought back as
  ``delta.reasoning_content``. Verified live (2026-08): ``glm-5.3-flash`` accepts
  the param and returns ``reasoning_content`` / ``reasoning_tokens``.

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

import os
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

# Zhipu GLM model prefixes that accept the ``thinking`` param on the bigmodel
# OpenAI-compatible API. Legacy glm-4 / glm-4v families predate it and reject
# the param with a 400, so they stay excluded.
_ZHIPU_REASONING_PREFIXES = ("glm-4.5", "glm-4.6", "glm-5")

# Reasoning token budget for Anthropic's ``thinking`` param. Must stay well under
# the model's ``max_tokens`` or the API rejects the request.
_DEFAULT_ANTHROPIC_BUDGET = 2000

# Thinking-token headroom for the providers that draw reasoning tokens from the
# SHARED ``max_tokens`` pool (DeepSeek / GLM / OpenAI reasoning models — none of
# them expose a budget knob). When thinking is enabled the model config inflates
# ``max_tokens`` by this much so the visible answer is not squeezed to nothing.
_DEFAULT_NON_ANTHROPIC_BUDGET = int(os.getenv("MAIN_LLM_THINKING_BUDGET", "4096"))

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


def is_zhipu_reasoning_model(model_name: str) -> bool:
    """Return True iff ``model_name`` is a GLM model that accepts ``thinking``.

    Mirrors ``is_openai_reasoning_model``: any ``org/model`` prefix is stripped
    so router-style names (e.g. ``zhipu/glm-4.6``) still match. Covers the
    thinking-capable GLM series (glm-4.5+, glm-4.6, glm-5.x incl. flash
    variants); legacy glm-4 / glm-4v families return False.
    """
    name = model_name.lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name.startswith(_ZHIPU_REASONING_PREFIXES)


def get_thinking_budget(provider: str | None, model_name: str | None, enabled: bool) -> int:
    """Return the reasoning-token headroom the model config must add to ``max_tokens``.

    Thinking tokens and output tokens share the ``max_tokens`` budget: without
    extra headroom a reasoning model can spend the whole cap thinking and cut
    the visible answer to nothing. Anthropic reports the budget explicitly
    (``budget_tokens``, reasoning models only); every other payload style
    draws from the shared pool, so a fixed budget is added. Returns 0 when
    the switch is off, the provider is unknown, or the model does not accept
    a reasoning payload — mirroring :func:`build_reasoning_kwargs` exactly so
    the two never disagree about "does this call think?".
    """
    if not enabled or not provider:
        return 0
    provider = provider.strip().lower()
    if provider == "anthropic":
        if model_name and _is_anthropic_reasoning_model(model_name):
            return _DEFAULT_ANTHROPIC_BUDGET
        return 0
    if provider == "deepseek":
        return _DEFAULT_NON_ANTHROPIC_BUDGET
    if provider in _OPENAI_COMPATIBLE:
        if model_name and (
            is_zhipu_reasoning_model(model_name) or is_openai_reasoning_model(model_name)
        ):
            return _DEFAULT_NON_ANTHROPIC_BUDGET
        return 0
    return 0


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
        # Zhipu GLM through an OpenAI-compatible gateway (bigmodel v4 API): the
        # DeepSeek-style request-body key ``thinking`` toggles chain-of-thought
        # and the API streams ``delta.reasoning_content``. Inject ONLY for the
        # thinking-capable GLM series; legacy glm-4 models reject it with a 400.
        if model_name and is_zhipu_reasoning_model(model_name):
            return {"extra_body": {"thinking": {"type": "enabled"}}}
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


__all__ = [
    "build_reasoning_kwargs",
    "get_thinking_budget",
    "is_openai_reasoning_model",
    "is_zhipu_reasoning_model",
]
