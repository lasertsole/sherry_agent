"""Provider-correct reasoning kwargs — declarative ``thinkingLevelMap`` engine.

``MAIN_LLM_ENABLE_THINKING`` is one universal thinking switch; each provider
exposes its reasoning mode under a different payload format. This module turns
that into a **declarative** lookup (modelled on pi's ``thinkingLevelMap`` +
``thinkingFormat``) instead of the old per-provider strategy chain:

* ``resolve_thinking_format`` picks the WIRE SHAPE from ``(provider, model)``;
* ``resolve_thinking_level_map`` picks the per-level wire VALUES (a model
  family's declarations override the format's default map);
* ``clamp_thinking_level`` lands a requested level on the nearest level the
  model actually has (up first, then down — pi's rule);
* ``build_thinking_kwargs`` is the single entry that composes the three.

Native providers (Anthropic, Google, Bedrock) do not go through a format: their
client libraries take first-class kwargs and keep their own branches.

Compatibility is part of the contract — every payload pinned by
``tests/models/LLMs/test_reasoning_payload.py`` and
``test_main_llm_thinking_variants.py`` stays byte-identical:

* the level a plain ON maps to (``THINKING_LEVEL_CONFIG["default_level"]``,
  ``"high"``) resolves to the legacy ``enabled`` payload on the DeepSeek/ZAI
  formats, so a toggle-on request sends exactly what it always sent;
* formats whose API cannot express "no thinking" (OpenAI reasoning models)
  answer an OFF request with ``{}`` — an absent param, the pre-existing best
  effort;
* an OFF request for an always-think model (``off: None`` on a format that
  needs an explicit level) resolves to the lowest supported level — the
  "floor" the thinking control used to learn only after a gateway rejection.

Legacy entry points (``build_reasoning_kwargs`` and friends) remain as thin
wrappers so every existing call site keeps working unchanged.
"""

from __future__ import annotations

from typing import Any

from config.features import REASONING_BUDGET
from config.features.agent_side.thinking_levels import (
    EXTENDED_THINKING_LEVELS,
    THINKING_LEVEL_CONFIG,
    ThinkingFormat,
)

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
    "together",
    "baseten",
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

#: The level a plain ON maps to (kept in config so the client and the backend
#: agree on what "thinking on" means).
_DEFAULT_ON_LEVEL = THINKING_LEVEL_CONFIG["default_level"]


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


def _bare_name(model_name: str | None) -> str:
    """The model name with any ``org/`` router prefix stripped, lowercased."""
    if not model_name:
        return ""
    name = model_name.lower()
    return name.rsplit("/", 1)[-1] if "/" in name else name


# ---------------------------------------------------------------------------
# The declarative layer: format selection + per-level maps
# ---------------------------------------------------------------------------

#: Default per-level wire maps, one per OpenAI-compatible format. A model
#: family's declaration (``_MODEL_LEVEL_OVERRIDES``) replaces entries per level.
#: ``high`` is the legacy ON payload for the "enabled/disabled" formats, so a
#: plain toggle-on stays byte-identical.
_FORMAT_LEVEL_MAPS: dict[str, dict[str, str | None]] = {
    ThinkingFormat.OPENAI.value: {
        "off": None,
        # "minimal" exists on gpt-5 but not on the o-series; "low" is the
        # lowest effort every reasoning model here accepts.
        "minimal": "low",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "high",
        "max": "high",
    },
    ThinkingFormat.DEEPSEEK.value: {
        "off": "disabled",
        "minimal": "low",
        "low": "low",
        "medium": "medium",
        "high": "enabled",
        "xhigh": "high",
        "max": "high",
    },
    ThinkingFormat.ZAI.value: {
        "off": "disabled",
        "minimal": "low",
        "low": "low",
        # "medium" is NOT a wire value the bigmodel API accepts as a type — the
        # clamp steps a medium request up to high instead.
        "medium": None,
        "high": "enabled",
        "xhigh": "max",
        "max": "max",
    },
    ThinkingFormat.QWEN.value: {
        "off": "disabled",
        "minimal": "low",
        "low": "low",
        "medium": "medium",
        "high": "enabled",
        "xhigh": "high",
        "max": "high",
    },
    ThinkingFormat.TOGETHER.value: {
        "off": "disabled",
        "minimal": "low",
        "low": "low",
        "medium": "medium",
        "high": "enabled",
        "xhigh": "high",
        "max": "high",
    },
    ThinkingFormat.BASETEN.value: {
        "off": "disabled",
        "minimal": "low",
        "low": "low",
        "medium": "medium",
        "high": "enabled",
        "xhigh": "high",
        "max": "high",
    },
    ThinkingFormat.OPENROUTER.value: {
        "off": "none",
        "minimal": "minimal",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "high",
        "max": "high",
    },
    ThinkingFormat.CHAT_TEMPLATE.value: {
        "off": "disabled",
        "minimal": "low",
        "low": "low",
        "medium": "medium",
        "high": "enabled",
        "xhigh": "high",
        "max": "high",
    },
    ThinkingFormat.QWEN_CHAT_TEMPLATE.value: {
        "off": "disabled",
        "minimal": "low",
        "low": "low",
        "medium": "medium",
        "high": "enabled",
        "xhigh": "high",
        "max": "high",
    },
    ThinkingFormat.STRING_THINKING.value: {
        "off": "off",
        "minimal": "low",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "high",
        "max": "high",
    },
    ThinkingFormat.ANT_LING.value: {
        "off": "none",
        "minimal": "low",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "high",
        "max": "high",
    },
}

#: Per-model-family overrides on top of the format defaults. Only levels the
#: family is KNOWN to differ on are listed; ``None`` = the level does not exist
#: for that family (the clamp skips it).
_MODEL_LEVEL_OVERRIDES: dict[str, dict[str, str | None]] = {
    # Always-think GLM-5 series: no disable, and the gateway documents exactly
    # low / high / max (error code 1210's own wording). Mid-levels do not exist.
    "glm-5": {"off": None, "minimal": None, "medium": None, "xhigh": None},
    # GLM-4.5 / 4.6: the enable/disable switch plus low / high / max levels.
    "glm-4.5": {"minimal": None, "medium": None, "xhigh": None},
    "glm-4.6": {"minimal": None, "medium": None, "xhigh": None},
}


def resolve_thinking_format(provider: str | None, model_name: str | None) -> str | None:
    """Pick the wire format for ``(provider, model)`` — None when unsupported.

    Native providers (``anthropic`` / ``google`` / ``bedrock``) resolve to their
    own name and are handled outside the OpenAI-compatible dispatcher.
    """
    if not provider:
        return None
    provider = provider.strip().lower()
    if provider == "deepseek":
        return ThinkingFormat.DEEPSEEK.value
    if provider in ("anthropic", "bedrock", "google", "google-genai", "gemini", "vertexai"):
        return provider
    if provider not in _OPENAI_COMPATIBLE:
        return None
    model = _bare_name(model_name)
    if model.startswith(_ZHIPU_REASONING_PREFIXES):
        return ThinkingFormat.ZAI.value
    if provider == "dashscope":
        return ThinkingFormat.QWEN.value
    if provider == "openrouter":
        return ThinkingFormat.OPENROUTER.value
    if provider == "together":
        return ThinkingFormat.TOGETHER.value
    if provider == "baseten":
        return ThinkingFormat.BASETEN.value
    if model_name and is_openai_reasoning_model(model_name):
        return ThinkingFormat.OPENAI.value
    return None


def resolve_thinking_level_map(
    provider: str | None,
    model_name: str | None,
    *,
    thinking_format: str | None = None,
) -> dict[str, str | None] | None:
    """The per-level wire map for ``(provider, model)``, or None when unsupported.

    Format defaults overlaid by the model family's overrides — the declarative
    half of this module. An explicit ``thinking_format`` overrides the format
    resolution (the custom-wire seam): its defaults then supply the map.
    ``None`` (no format) means the switch maps to a no-op for this pair, exactly
    like the old strategy chain's ``_NoOpStrategy``.
    """
    thinking_format = thinking_format or resolve_thinking_format(provider, model_name)
    if thinking_format is None or thinking_format not in _FORMAT_LEVEL_MAPS:
        return None
    level_map = dict(_FORMAT_LEVEL_MAPS[thinking_format])
    model = _bare_name(model_name)
    for prefix, overrides in _MODEL_LEVEL_OVERRIDES.items():
        if model.startswith(prefix):
            level_map.update(overrides)
            break
    return level_map


def get_supported_thinking_levels(provider: str | None, model_name: str | None) -> list[str]:
    """The levels this model accepts, in clamp order (``off`` … ``max``).

    A level is supported when the resolved map declares a wire value for it; an
    ``off`` of ``None`` (always-think / no disable wire) is NOT supported. An
    entirely unmapped pair reports ``["off"]`` — pi's fallback: a model we know
    nothing about gets the one level that sends nothing.
    """
    level_map = resolve_thinking_level_map(provider, model_name)
    if level_map is None:
        return ["off"]
    return [level for level in EXTENDED_THINKING_LEVELS if level_map.get(level) is not None]


def clamp_thinking_level(provider: str | None, model_name: str | None, level: str) -> str:
    """Land ``level`` on the nearest level ``(provider, model)`` actually has.

    pi's rule: an exact hit wins; otherwise search UP first (more thinking),
    then DOWN; a pair with no supported level clamps to ``off``. An unknown
    token is returned untouched (callers validate their own input).
    """
    supported = get_supported_thinking_levels(provider, model_name)
    if level in supported:
        return level
    if level not in EXTENDED_THINKING_LEVELS:
        return level
    index = EXTENDED_THINKING_LEVELS.index(level)
    for candidate in EXTENDED_THINKING_LEVELS[index + 1 :]:
        if candidate in supported:
            return candidate
    for candidate in reversed(EXTENDED_THINKING_LEVELS[:index]):
        if candidate in supported:
            return candidate
    return supported[0] if supported else "off"


# ---------------------------------------------------------------------------
# The dispatcher: level -> wire kwargs
# ---------------------------------------------------------------------------


def _wire_payload(thinking_format: str, wire_value: str) -> dict[str, Any]:
    """The request fragment for one ``(format, wire value)`` pair.

    Wire values ``enabled`` / ``disabled`` / ``none`` / ``off`` are treated as
    state switches by the formats that carry a separate bool/effort field.
    """
    if thinking_format == ThinkingFormat.DEEPSEEK.value:
        # DeepSeek's thinking.type accepts enabled/disabled only; an effort
        # level is carried by the separate reasoning_effort field (pi's own
        # deepseek rule), with the type left enabled.
        if wire_value in ("enabled", "disabled"):
            return {"extra_body": {"thinking": {"type": wire_value}}}
        return {
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": wire_value,
        }
    if thinking_format == ThinkingFormat.ZAI.value:
        return {"extra_body": {"thinking": {"type": wire_value}}}
    if thinking_format == ThinkingFormat.OPENAI.value:
        return {"reasoning_effort": wire_value}
    if thinking_format == ThinkingFormat.OPENROUTER.value:
        return {"reasoning": {"effort": wire_value}}
    if thinking_format in (ThinkingFormat.TOGETHER.value, ThinkingFormat.BASETEN.value):
        enabled = wire_value not in ("disabled", "none", "off", "")
        payload: dict[str, Any] = {"reasoning": {"enabled": enabled}}
        if wire_value not in ("enabled", "disabled", "none", "off", ""):
            payload["reasoning_effort"] = wire_value
        return payload
    if thinking_format in (ThinkingFormat.QWEN.value, ThinkingFormat.QWEN_CHAT_TEMPLATE.value):
        enabled = wire_value not in ("disabled", "none", "off", "")
        payload = {"extra_body": {"enable_thinking": enabled}}
        if wire_value not in ("enabled", "disabled", "none", "off", ""):
            payload["reasoning_effort"] = wire_value
        return payload
    if thinking_format == ThinkingFormat.CHAT_TEMPLATE.value:
        enabled = wire_value not in ("disabled", "none", "off", "")
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": enabled}}}
    if thinking_format == ThinkingFormat.STRING_THINKING.value:
        return {"extra_body": {"thinking": wire_value}}
    if thinking_format == ThinkingFormat.ANT_LING.value:
        return {"reasoning": {"effort": wire_value}} if wire_value not in ("none", "") else {}
    return {}


def _native_thinking_kwargs(provider: str, model_name: str | None, level: str) -> dict[str, Any]:
    """Anthropic / Google / Bedrock branches (first-class client kwargs)."""
    if provider == "anthropic":
        if not model_name or not _is_anthropic_reasoning_model(model_name):
            return {}
        if level == "off":
            # Anthropic reasoning defaults to OFF: an absent param disables it.
            return {}
        # Every enabled level sends the documented budget block: Anthropic has
        # no named effort levels (adaptive/managed-effort are future work).
        return {"thinking": {"type": "enabled", "budget_tokens": _DEFAULT_ANTHROPIC_BUDGET}}
    if provider in ("google", "google-genai", "gemini", "vertexai"):
        if not model_name or level == "off":
            # Gemini's thinking cannot be disabled; absent param = server default.
            return {}
        # The discrete form (Gemini 3+) with the documented four levels.
        level_map = {
            "minimal": "MINIMAL",
            "low": "LOW",
            "medium": "MEDIUM",
            "high": "HIGH",
            "xhigh": "HIGH",
            "max": "HIGH",
        }
        wire = level_map.get(level)
        return {"thinking_level": wire} if wire else {}
    return {}


def build_thinking_kwargs(
    provider: str | None,
    model_name: str | None,
    level: str,
    *,
    thinking_format: str | None = None,
    thinking_level_map: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """The reasoning kwargs for one ``(provider, model, level)`` triple.

    The single entry point of the declarative engine:

    1. resolve the format and the level map (explicit arguments win — the seam
       a future custom-wire feature uses);
    2. CLAMP the requested level onto what the model has;
    3. dispatch on the format.

    ``off`` has three resolutions, by format capability:

    * the map carries a wire value (DeepSeek / ZAI / OpenRouter …) → send it;
    * the map says ``off: None`` and the format cannot express "less thinking"
      (OpenAI reasoning models) → ``{}``, an absent param (the pre-existing
      best effort);
    * the map says ``off: None`` on an always-think gateway (GLM-5) → clamp UP
      to the lowest supported level, i.e. the "floor" the thinking control used
      to discover only after a 400.

    Never raises, never injects an unsupported param (a resolved level with no
    wire value returns ``{}``).
    """
    if not provider:
        return {}
    provider = provider.strip().lower()
    if provider in ("anthropic", "google", "google-genai", "gemini", "vertexai", "bedrock"):
        return _native_thinking_kwargs(provider, model_name, level)
    thinking_format = thinking_format or resolve_thinking_format(provider, model_name)
    if thinking_format is None:
        return {}
    level_map = (
        thinking_level_map
        if thinking_level_map is not None
        else resolve_thinking_level_map(provider, model_name, thinking_format=thinking_format)
    )
    if level_map is None:
        return {}
    if level == "off":
        wire = level_map.get("off")
        if wire is None:
            if thinking_format == ThinkingFormat.OPENAI.value:
                return {}  # no disable wire: absent param is the best effort
            level = clamp_thinking_level(provider, model_name, "off")
            wire = level_map.get(level)
        return _wire_payload(thinking_format, str(wire)) if wire is not None else {}
    clamped = clamp_thinking_level(provider, model_name, level)
    wire = level_map.get(clamped)
    if wire is None:
        return {}
    return _wire_payload(thinking_format, str(wire))


# ---------------------------------------------------------------------------
# Legacy entry points (thin wrappers over the engine)
# ---------------------------------------------------------------------------


def build_reasoning_kwargs(
    provider: str | None,
    model_name: str | None,
    enabled: bool,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Return kwargs to merge into ``init_chat_model(...)`` for reasoning mode.

    Backwards-compatible wrapper: ``enabled`` maps to the ON level (an explicit
    ``reasoning_effort`` is honoured for the OpenAI format) or to ``off``.
    Returns ``{}`` (never ``None``) when the switch is off or the provider/model
    does not accept a reasoning payload — never raises, never injects an
    unsupported parameter.
    """
    if not enabled or not provider:
        return {}
    if reasoning_effort and reasoning_effort.strip().lower() in _VALID_REASONING_EFFORTS:
        effort = reasoning_effort.strip().lower()
        if resolve_thinking_format(provider, model_name) == ThinkingFormat.OPENAI.value:
            return {"reasoning_effort": effort}
    return build_thinking_kwargs(provider, model_name, _DEFAULT_ON_LEVEL)


def build_thinking_off_kwargs(provider: str | None, model_name: str | None) -> dict[str, Any]:
    """Return kwargs that explicitly DISABLE thinking for the provider/model.

    Needed for gateways whose server-side default is thinking ON (Zhipu GLM):
    an absent param would leave the server default in charge. Providers that
    default to OFF (Anthropic, o-series) get ``{}`` — the same never-crash
    contract as :func:`build_reasoning_kwargs`; always-think models resolve to
    their lowest supported level (see :func:`build_thinking_kwargs`).
    """
    if not provider:
        return {}
    return build_thinking_kwargs(provider, model_name, "off")


def build_thinking_floor_kwargs(provider: str | None, model_name: str | None) -> dict[str, Any]:
    """Return kwargs for the MINIMUM thinking level of always-think models.

    The caller decides when that applies (``thinking_control_mode`` raises the
    floor only for the always-think models). The floor FORCES a level — asking
    for ``off`` would let a model that does have a disable wire (glm-4.6) answer
    with its disabled payload, which is not what a floor means. The lowest
    effort level is the clamp of ``minimal`` (unsupported mid-levels step up).
    """
    if not provider or not model_name:
        return {}
    return build_thinking_kwargs(provider, model_name, "minimal")


def build_thinking_level_kwargs(
    provider: str | None, model_name: str | None, level: str
) -> dict[str, Any]:
    """Return kwargs that pin thinking to an explicit LEVEL.

    Any of the seven levels; the clamp lands it on what the model has, so a
    level the model does not accept can never be sent (the old behaviour: it
    was sent and the gateway answered 400).
    """
    if not provider or not model_name:
        return {}
    return build_thinking_kwargs(provider, model_name, level)


def thinking_control_mode(provider: str | None, model_name: str | None) -> str:
    """How this model's thinking is controlled: ``"on_off"`` or ``"levels"``.

    ``levels`` marks always-think models that reject the disable payload and
    only accept a thinking level (verified live 2026-09: glm-5.3-flash, error
    code 1210). The client renders a level selector for them instead of a
    switch. (Deliberately NOT derived from "is off supported": o-series and
    Anthropic models have no disable wire yet keep the switch — the switch's
    off state is a param-free request for them, which is the shipped
    behaviour.)
    """
    if not provider or not model_name:
        return "on_off"
    name = _bare_name(model_name)
    if provider.strip().lower() in _OPENAI_COMPATIBLE and name.startswith(("glm-5",)):
        return "levels"
    return "on_off"


def is_thinking_disable_rejection(exc: BaseException) -> bool:
    """True when a gateway rejected the thinking-off payload for an
    always-thinks model (the "use low/high/max instead" 400 family)."""
    text = str(exc)
    return ("不支持关闭思考" in text) or ("始终思考" in text)


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
    if resolve_thinking_level_map(provider, model_name) is None:
        return 0
    return _DEFAULT_NON_ANTHROPIC_BUDGET


__all__ = [
    "build_reasoning_kwargs",
    "build_thinking_floor_kwargs",
    "build_thinking_kwargs",
    "build_thinking_level_kwargs",
    "build_thinking_off_kwargs",
    "clamp_thinking_level",
    "get_supported_thinking_levels",
    "get_thinking_budget",
    "is_openai_reasoning_model",
    "is_thinking_disable_rejection",
    "is_zhipu_reasoning_model",
    "resolve_thinking_format",
    "resolve_thinking_level_map",
    "thinking_control_mode",
]
