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

from collections.abc import Callable
from dataclasses import dataclass
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


#: Providers whose client libraries take first-class reasoning kwargs instead
#: of a wire payload. The resolved "format" is the provider name itself.
_NATIVE_FORMATS = frozenset(
    {"anthropic", "bedrock", "google", "google-genai", "gemini", "vertexai"}
)


@dataclass(frozen=True)
class _FormatRule:
    """One row of the format Strategy table.

    A rule matches a ``(provider, model)`` pair and names the wire format. Adding
    a gateway is a registration (2026-11-09 audit H1), not three new ``elif``
    branches across the module.
    """

    thinking_format: str
    providers: frozenset[str] | None = None
    model_prefixes: tuple[str, ...] = ()
    requires_reasoning_model: bool = False

    def matches(self, provider: str, model: str) -> bool:
        if self.providers is not None and provider not in self.providers:
            return False
        if self.model_prefixes and not model.startswith(self.model_prefixes):
            return False
        if self.requires_reasoning_model and not is_openai_reasoning_model(model):
            return False
        return True


#: Ordered rules — first match wins. ZAI's model-prefix rule sits before the
#: provider-specific OpenAI-compatible rules on purpose: a GLM model routed
#: through any compatible gateway keeps its ZAI wire shape.
_FORMAT_RULES: tuple[_FormatRule, ...] = (
    _FormatRule(ThinkingFormat.DEEPSEEK.value, providers=frozenset({"deepseek"})),
    _FormatRule(
        ThinkingFormat.ZAI.value,
        providers=frozenset(_OPENAI_COMPATIBLE),
        model_prefixes=_ZHIPU_REASONING_PREFIXES,
    ),
    _FormatRule(ThinkingFormat.QWEN.value, providers=frozenset({"dashscope"})),
    _FormatRule(ThinkingFormat.OPENROUTER.value, providers=frozenset({"openrouter"})),
    _FormatRule(ThinkingFormat.TOGETHER.value, providers=frozenset({"together"})),
    _FormatRule(ThinkingFormat.BASETEN.value, providers=frozenset({"baseten"})),
    _FormatRule(
        ThinkingFormat.OPENAI.value,
        providers=frozenset(_OPENAI_COMPATIBLE),
        requires_reasoning_model=True,
    ),
)


def resolve_thinking_format(provider: str | None, model_name: str | None) -> str | None:
    """Pick the wire format for ``(provider, model)`` — None when unsupported.

    Native providers resolve to their own name (handled outside the
    OpenAI-compatible dispatcher); everything else walks :data:`_FORMAT_RULES`.
    """
    if not provider:
        return None
    provider = provider.strip().lower()
    if provider in _NATIVE_FORMATS:
        return provider
    model = _bare_name(model_name)
    for rule in _FORMAT_RULES:
        if rule.matches(provider, model):
            return rule.thinking_format
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


def get_supported_thinking_levels(
    provider: str | None,
    model_name: str | None,
    *,
    custom_mapping: dict[str, Any] | None = None,
) -> list[str]:
    """The levels this model accepts, in clamp order (``off`` … ``max``).

    A level is supported when the resolved map declares a wire value for it; an
    ``off`` of ``None`` (always-think / no disable wire) is NOT supported. An
    entirely unmapped pair reports ``["off"]`` — pi's fallback: a model we know
    nothing about gets the one level that sends nothing.
    """
    if custom_mapping and custom_mapping.get("level_map"):
        level_map = dict(custom_mapping["level_map"])
        return [lvl for lvl in EXTENDED_THINKING_LEVELS if level_map.get(lvl) is not None]
    level_map = resolve_thinking_level_map(provider, model_name)
    if level_map is None:
        return ["off"]
    return [level for level in EXTENDED_THINKING_LEVELS if level_map.get(level) is not None]


def clamp_thinking_level(
    provider: str | None,
    model_name: str | None,
    level: str,
    *,
    custom_mapping: dict[str, Any] | None = None,
) -> str:
    """Land ``level`` on the nearest level ``(provider, model)`` actually has.

    pi's rule: an exact hit wins; otherwise search UP first (more thinking),
    then DOWN; a pair with no supported level clamps to ``off``. An unknown
    token is returned untouched (callers validate their own input).
    """
    supported = get_supported_thinking_levels(provider, model_name, custom_mapping=custom_mapping)
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


def _nested_wire_field(path: str, value: str) -> dict[str, Any]:
    """Build ``{"a": {"b": value}}`` from a dotted ``a.b`` wire field.

    Empty segments are skipped, so a malformed path degrades to a shallower
    fragment rather than raising (the API boundary validates the path first).
    """
    segments = [segment for segment in path.split(".") if segment]
    payload: dict[str, Any] = {}
    cursor = payload
    for segment in segments[:-1]:
        nxt: dict[str, Any] = {}
        cursor[segment] = nxt
        cursor = nxt
    if segments:
        cursor[segments[-1]] = value
    return payload


def _custom_mapping_payload(custom_mapping: dict[str, Any], wire_value: str) -> dict[str, Any]:
    """The fragment for a session's custom mapping at one wire value.

    A known ``wire_format`` reuses that format's shape; ``"custom"`` (or a
    missing format) writes the value at the mapping's own dotted ``wire_field``.
    """
    wire_format = str(custom_mapping.get("wire_format") or "custom")
    if wire_format in _WIRE_ENCODERS:
        return _wire_payload(wire_format, wire_value)
    wire_field = str(custom_mapping.get("wire_field") or "")
    if not wire_field:
        return {}
    return _nested_wire_field(wire_field, wire_value)


def _resolved_level_map(
    provider: str | None,
    model_name: str | None,
    thinking_format: str | None,
    thinking_level_map: dict[str, str | None] | None,
    custom_mapping: dict[str, Any] | None,
) -> tuple[str | None, dict[str, str | None] | None]:
    """(format, level map) with the custom mapping taking precedence.

    A custom mapping's own ``wire_format`` overrides the provider-resolved one,
    and its ``level_map`` replaces the declarative map entirely — the escape
    hatch is meant to win.
    """
    if custom_mapping and custom_mapping.get("level_map"):
        custom_level_map = dict(custom_mapping["level_map"])
        custom_format = custom_mapping.get("wire_format")
        if isinstance(custom_format, str) and custom_format == "custom":
            custom_format = None
        resolved = custom_format or thinking_format or resolve_thinking_format(provider, model_name)
        return resolved, custom_level_map
    resolved = thinking_format or resolve_thinking_format(provider, model_name)
    if thinking_level_map is not None:
        return resolved, dict(thinking_level_map)
    return resolved, resolve_thinking_level_map(provider, model_name, thinking_format=resolved)


#: Wire values that mean "off" for the formats carrying a bool/effort field.
_OFF_WIRE_VALUES = frozenset({"disabled", "none", "off", ""})


def _encode_deepseek(wire_value: str) -> dict[str, Any]:
    # ``thinking.type`` accepts enabled/disabled only; an effort level is carried
    # by the separate ``reasoning_effort`` field (pi's own deepseek rule), with
    # the type left enabled.
    if wire_value in ("enabled", "disabled"):
        return {"extra_body": {"thinking": {"type": wire_value}}}
    return {"extra_body": {"thinking": {"type": "enabled"}}, "reasoning_effort": wire_value}


def _encode_zai(wire_value: str) -> dict[str, Any]:
    return {"extra_body": {"thinking": {"type": wire_value}}}


def _encode_openai_effort(wire_value: str) -> dict[str, Any]:
    return {"reasoning_effort": wire_value}


def _encode_openrouter(wire_value: str) -> dict[str, Any]:
    return {"reasoning": {"effort": wire_value}}


def _encode_boolean_reasoning(wire_value: str) -> dict[str, Any]:
    """Together / Baseten: ``reasoning.enabled`` plus an optional effort."""
    enabled = wire_value not in _OFF_WIRE_VALUES
    payload: dict[str, Any] = {"reasoning": {"enabled": enabled}}
    if wire_value not in _OFF_WIRE_VALUES and wire_value != "enabled":
        payload["reasoning_effort"] = wire_value
    return payload


def _encode_qwen(wire_value: str) -> dict[str, Any]:
    """Qwen: ``enable_thinking`` plus an optional effort."""
    enabled = wire_value not in _OFF_WIRE_VALUES
    payload: dict[str, Any] = {"extra_body": {"enable_thinking": enabled}}
    if wire_value not in _OFF_WIRE_VALUES and wire_value != "enabled":
        payload["reasoning_effort"] = wire_value
    return payload


def _encode_chat_template(wire_value: str) -> dict[str, Any]:
    enabled = wire_value not in _OFF_WIRE_VALUES
    return {"extra_body": {"chat_template_kwargs": {"enable_thinking": enabled}}}


def _encode_string_thinking(wire_value: str) -> dict[str, Any]:
    return {"extra_body": {"thinking": wire_value}}


def _encode_ant_ling(wire_value: str) -> dict[str, Any]:
    return {"reasoning": {"effort": wire_value}} if wire_value not in ("none", "") else {}


def _encode_nothing(wire_value: str) -> dict[str, Any]:
    return {}


#: Format -> wire encoder. Adding a format registers one function here (H1).
_WIRE_ENCODERS: dict[str, Callable[[str], dict[str, Any]]] = {
    ThinkingFormat.DEEPSEEK.value: _encode_deepseek,
    ThinkingFormat.ZAI.value: _encode_zai,
    ThinkingFormat.OPENAI.value: _encode_openai_effort,
    ThinkingFormat.OPENROUTER.value: _encode_openrouter,
    ThinkingFormat.TOGETHER.value: _encode_boolean_reasoning,
    ThinkingFormat.BASETEN.value: _encode_boolean_reasoning,
    ThinkingFormat.QWEN.value: _encode_qwen,
    ThinkingFormat.QWEN_CHAT_TEMPLATE.value: _encode_qwen,
    ThinkingFormat.CHAT_TEMPLATE.value: _encode_chat_template,
    ThinkingFormat.STRING_THINKING.value: _encode_string_thinking,
    ThinkingFormat.ANT_LING.value: _encode_ant_ling,
}


def _wire_payload(thinking_format: str, wire_value: str) -> dict[str, Any]:
    """The request fragment for one ``(format, wire value)`` pair.

    Wire values ``enabled`` / ``disabled`` / ``none`` / ``off`` are treated as
    state switches by the formats that carry a separate bool/effort field.
    """
    return _WIRE_ENCODERS.get(thinking_format, _encode_nothing)(wire_value)


#: Anthropic models that take the ADAPTIVE thinking form
#: (``thinking.type: "adaptive"`` + ``output_config.effort``) instead of the
#: legacy token budget. Declared per prefix; a model outside the list keeps the
#: budget form.
_ANTHROPIC_ADAPTIVE_PREFIXES = (
    "claude-opus-4-5",
    "claude-opus-4-6",
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
    "claude-4-5",
    "claude-4-6",
)

#: Level -> the three named efforts Anthropic accepts.
_ANTHROPIC_EFFORT = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


def _anthropic_thinking_kwargs(model_name: str | None, level: str) -> dict[str, Any]:
    """Anthropic's thinking payload for one model + level.

    Adaptive-capable models (``_ANTHROPIC_ADAPTIVE_PREFIXES``) carry an effort
    via ``output_config``; everything else keeps the documented token budget.
    ``off`` is an absent param -- the API's own default is OFF.
    """
    if not model_name or not _is_anthropic_reasoning_model(model_name):
        return {}
    if level == "off":
        return {}
    name = _bare_name(model_name)
    if name.startswith(_ANTHROPIC_ADAPTIVE_PREFIXES):
        effort = _ANTHROPIC_EFFORT.get(level, "high")
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}
    return {"thinking": {"type": "enabled", "budget_tokens": _DEFAULT_ANTHROPIC_BUDGET}}


#: Bedrock carries Claude's thinking under ``additional_model_request_fields``
#: and GPT's effort as a first-class param; the model name decides.
def _bedrock_thinking_kwargs(model_name: str | None, level: str) -> dict[str, Any]:
    if not model_name or level == "off":
        return {}
    name = _bare_name(model_name)
    if "claude" in name:
        effort = _ANTHROPIC_EFFORT.get(level, "high")
        return {
            "additional_model_request_fields": {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": effort},
            }
        }
    if "gpt" in name or "openai" in name:
        return {"reasoning_effort": _ANTHROPIC_EFFORT.get(level, "high")}
    return {}


#: The discrete Gemini form (Gemini 3+) with the documented four levels.
_GOOGLE_THINKING_LEVELS: dict[str, str] = {
    "minimal": "MINIMAL",
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
    "xhigh": "HIGH",
    "max": "HIGH",
}


def _google_thinking_kwargs(model_name: str | None, level: str) -> dict[str, Any]:
    if not model_name or level == "off":
        # Gemini's thinking cannot be disabled; absent param = server default.
        return {}
    wire = _GOOGLE_THINKING_LEVELS.get(level)
    return {"thinking_level": wire} if wire else {}


#: Native provider -> first-class kwargs encoder. The resolved "format" of a
#: native provider is its own name, so these keys and the resolver agree.
_NATIVE_ENCODERS: dict[str, Callable[[str | None, str], dict[str, Any]]] = {
    "anthropic": _anthropic_thinking_kwargs,
    "bedrock": _bedrock_thinking_kwargs,
    "google": _google_thinking_kwargs,
    "google-genai": _google_thinking_kwargs,
    "gemini": _google_thinking_kwargs,
    "vertexai": _google_thinking_kwargs,
}


def _native_thinking_kwargs(provider: str, model_name: str | None, level: str) -> dict[str, Any]:
    """Anthropic / Google / Bedrock branches (first-class client kwargs)."""
    encoder = _NATIVE_ENCODERS.get(provider)
    return encoder(model_name, level) if encoder is not None else {}


def build_thinking_kwargs(
    provider: str | None,
    model_name: str | None,
    level: str,
    *,
    thinking_format: str | None = None,
    thinking_level_map: dict[str, str | None] | None = None,
    custom_mapping: dict[str, Any] | None = None,
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
    custom_active = bool(custom_mapping and custom_mapping.get("level_map"))
    if not custom_active and provider in (
        "anthropic",
        "google",
        "google-genai",
        "gemini",
        "vertexai",
        "bedrock",
    ):
        return _native_thinking_kwargs(provider, model_name, level)
    resolved_format, level_map = _resolved_level_map(
        provider,
        model_name,
        thinking_format,
        thinking_level_map,
        custom_mapping,
    )
    if level_map is None or not level_map:
        return {}

    def _payload_for(wire: str) -> dict[str, Any]:
        if custom_active and custom_mapping is not None:
            return _custom_mapping_payload(custom_mapping, wire)
        return _wire_payload(str(resolved_format), wire)

    if level == "off":
        wire = level_map.get("off")
        if wire is None:
            if not custom_active and resolved_format == ThinkingFormat.OPENAI.value:
                return {}  # no disable wire: absent param is the best effort
            level = clamp_thinking_level(provider, model_name, "off", custom_mapping=custom_mapping)
            wire = level_map.get(level)
        return _payload_for(str(wire)) if wire is not None else {}
    clamped = clamp_thinking_level(provider, model_name, level, custom_mapping=custom_mapping)
    wire = level_map.get(clamped)
    if wire is None:
        return {}
    return _payload_for(str(wire))


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


def get_thinking_budget(
    provider: str | None,
    model_name: str | None,
    enabled: bool,
    *,
    level: str | None = None,
) -> int:
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
    if level:
        per_level = REASONING_BUDGET.get("thinking_budgets") or {}
        if level in per_level:
            return int(per_level[level])
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
