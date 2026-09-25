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

from typing import Any, Protocol

from config.features import REASONING_BUDGET

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
_DEFAULT_ANTHROPIC_BUDGET = REASONING_BUDGET["anthropic_default_thinking_budget"]

# Thinking-token headroom for the providers that draw reasoning tokens from the
# SHARED ``max_tokens`` pool (DeepSeek / GLM / OpenAI reasoning models — none of
# them expose a budget knob). When thinking is enabled the model config inflates
# ``max_tokens`` by this much so the visible answer is not squeezed to nothing.
# The ``MAIN_LLM_THINKING_BUDGET`` env override is resolved by the registry
# builder (config/features/agent_side.py) at import time.
_DEFAULT_NON_ANTHROPIC_BUDGET = REASONING_BUDGET["non_anthropic_default_thinking_budget"]

_VALID_REASONING_EFFORTS = ("low", "medium", "high")

# Thinking levels exposed by always-think gateways (glm-5 series et al).
_VALID_LEVELS = ("low", "high", "max")

# Zhipu GLM series that ALWAYS think and only accept a level (low/high/max),
# never the enabled/disabled switch. Verified live 2026-09: glm-5.3-flash
# rejects ``disabled`` with error code 1210.
_ALWAYS_THINK_PREFIXES = ("glm-5",)


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


class _ReasoningStrategy(Protocol):
    """Provider family's reasoning payload + shared-pool headroom.

    One strategy carries both operations so the two public functions can never
    disagree about whether a given provider/model reasons.
    """

    def build(self, model_name: str | None, reasoning_effort: str | None) -> dict[str, Any]: ...

    def thinking_budget(self, model_name: str | None) -> int: ...


class _NoOpStrategy:
    """Unknown providers: the universal switch maps to a documented no-op."""

    def build(self, model_name: str | None, reasoning_effort: str | None) -> dict[str, Any]:
        return {}

    def thinking_budget(self, model_name: str | None) -> int:
        return 0

    def disable(self, model_name: str | None) -> dict[str, Any]:
        return {}


class _DeepSeekStrategy:
    """DeepSeek V3.2+ chat API: thinking carried via ``extra_body``."""

    def build(self, model_name: str | None, reasoning_effort: str | None) -> dict[str, Any]:
        return {"extra_body": {"thinking": {"type": "enabled"}}}

    def thinking_budget(self, model_name: str | None) -> int:
        return _DEFAULT_NON_ANTHROPIC_BUDGET

    def disable(self, model_name: str | None) -> dict[str, Any]:
        return {"extra_body": {"thinking": {"type": "disabled"}}}


class _AnthropicStrategy:
    """``thinking`` is a first-class ``ChatAnthropic`` kwarg (reasoning models)."""

    def build(self, model_name: str | None, reasoning_effort: str | None) -> dict[str, Any]:
        if model_name and _is_anthropic_reasoning_model(model_name):
            return {
                "thinking": {
                    "type": "enabled",
                    "budget_tokens": _DEFAULT_ANTHROPIC_BUDGET,
                }
            }
        return {}

    def thinking_budget(self, model_name: str | None) -> int:
        if model_name and _is_anthropic_reasoning_model(model_name):
            return _DEFAULT_ANTHROPIC_BUDGET
        return 0

    def disable(self, model_name: str | None) -> dict[str, Any]:
        # Anthropic reasoning defaults to OFF: an absent ``thinking`` param
        # already disables it; sending an explicit disable block is not
        # supported by the API.
        return {}


class _OpenAICompatibleStrategy:
    """``reasoning_effort`` on OpenAI-compatible gateways (o-series / gpt-5).

    Zhipu GLM through the bigmodel v4 API is the exception: it takes the
    DeepSeek-style request-body ``thinking`` key instead, and only for the
    thinking-capable GLM series — whose SERVER-SIDE default is thinking ON,
    so forcing it off requires the explicit ``disabled`` payload.
    """

    def build(self, model_name: str | None, reasoning_effort: str | None) -> dict[str, Any]:
        if model_name and is_zhipu_reasoning_model(model_name):
            return {"extra_body": {"thinking": {"type": "enabled"}}}
        if model_name and is_openai_reasoning_model(model_name):
            effort = (reasoning_effort or "high").strip().lower()
            if effort in _VALID_REASONING_EFFORTS:
                return {"reasoning_effort": effort}
        return {}

    def thinking_budget(self, model_name: str | None) -> int:
        if model_name and (
            is_zhipu_reasoning_model(model_name) or is_openai_reasoning_model(model_name)
        ):
            return _DEFAULT_NON_ANTHROPIC_BUDGET
        return 0

    def disable(self, model_name: str | None) -> dict[str, Any]:
        if model_name and is_zhipu_reasoning_model(model_name):
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        # o-series / gpt-5 have no documented off switch for reasoning_effort;
        # an absent param keeps the gateway default (best effort).
        return {}


_NOOP_STRATEGY = _NoOpStrategy()

_REASONING_STRATEGIES: dict[str, _ReasoningStrategy] = {
    "deepseek": _DeepSeekStrategy(),
    "anthropic": _AnthropicStrategy(),
    **{name: _OpenAICompatibleStrategy() for name in _OPENAI_COMPATIBLE},
}


def _strategy_for(provider: str) -> _ReasoningStrategy:
    return _REASONING_STRATEGIES.get(provider, _NOOP_STRATEGY)


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
    return _strategy_for(provider.strip().lower()).thinking_budget(model_name)


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
    return _strategy_for(provider.strip().lower()).build(model_name, reasoning_effort)


def build_thinking_off_kwargs(provider: str | None, model_name: str | None) -> dict[str, Any]:
    """Return kwargs that explicitly DISABLE thinking for the provider/model.

    Needed for gateways whose server-side default is thinking ON (Zhipu GLM
    glm-4.5+): an absent param would leave the server default in charge, so
    the forced-off variant must send ``{"thinking": {"type": "disabled"}}``.
    Providers that default to OFF (Anthropic, o-series) get ``{}`` — the same
    never-crash contract as :func:`build_reasoning_kwargs`.
    """
    if not provider:
        return {}
    return _strategy_for(provider.strip().lower()).disable(model_name)


def build_thinking_floor_kwargs(provider: str | None, model_name: str | None) -> dict[str, Any]:
    """Return kwargs for the MINIMUM thinking level of always-think models.

    Some gateways (e.g. ``glm-5`` series, verified live 2026-09: glm-5.3-flash
    rejects ``disabled`` with error code 1210 "该模型始终思考，不支持关闭思考")
    cannot turn thinking off at all and only accept a level (low / high / max).
    For those the forced-off toggle maps to the lowest level — the closest
    possible approximation. Providers with a real off switch get ``{}``.
    """
    if not provider or not model_name:
        return {}
    name = model_name.lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if name.startswith(_ZHIPU_REASONING_PREFIXES):
        return {"extra_body": {"thinking": {"type": "low"}}}
    return {}


def build_thinking_level_kwargs(
    provider: str | None, model_name: str | None, level: str
) -> dict[str, Any]:
    """Return kwargs that pin thinking to an explicit LEVEL (low / high / max).

    For always-think models (e.g. ``glm-5`` series) the toggle is a level
    selector rather than an on/off switch. OpenAI reasoning models map the
    level onto ``reasoning_effort`` (``max`` collapses to ``high``); unknown
    providers get ``{}`` — the never-crash contract.
    """
    if level not in _VALID_LEVELS:
        return {}
    if not provider or not model_name:
        return {}
    name = model_name.lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if provider.strip().lower() in _OPENAI_COMPATIBLE and name.startswith(
        _ZHIPU_REASONING_PREFIXES
    ):
        return {"extra_body": {"thinking": {"type": level}}}
    if provider.strip().lower() in _OPENAI_COMPATIBLE and is_openai_reasoning_model(model_name):
        return {"reasoning_effort": "high" if level == "max" else level}
    return {}


def thinking_control_mode(provider: str | None, model_name: str | None) -> str:
    """How this model's thinking is controlled: ``"on_off"`` or ``"levels"``.

    ``levels`` marks always-think models that reject the disable payload and
    only accept a thinking level (verified live 2026-09: glm-5.3-flash, error
    code 1210 "该模型始终思考，不支持关闭思考；请使用 low、high 或 max"). The
    client renders a 低/高/最高 selector for them instead of a switch.
    """
    if not provider or not model_name:
        return "on_off"
    name = model_name.lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if provider.strip().lower() in _OPENAI_COMPATIBLE and name.startswith(_ALWAYS_THINK_PREFIXES):
        return "levels"
    return "on_off"


def is_thinking_disable_rejection(exc: BaseException) -> bool:
    """True when a gateway rejected the thinking-off payload for an
    always-thinks model (the "use low/high/max instead" 400 family)."""
    text = str(exc)
    return ("不支持关闭思考" in text) or ("始终思考" in text)


__all__ = [
    "build_reasoning_kwargs",
    "build_thinking_floor_kwargs",
    "build_thinking_level_kwargs",
    "build_thinking_off_kwargs",
    "get_thinking_budget",
    "is_openai_reasoning_model",
    "is_thinking_disable_rejection",
    "is_zhipu_reasoning_model",
    "thinking_control_mode",
]
