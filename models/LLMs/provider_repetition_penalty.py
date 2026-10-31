"""Per-provider sampling-time repetition penalty kwargs.

Mirrors :mod:`models.LLMs.provider_max_output`'s contract:

* a provider absent from the table is UNSUPPORTED — no field is passed (the
  provider keeps its own default);
* a ``None`` value means "if-set-then-pass": that kwarg is omitted;
* an unset per-family override falls back to :data:`REPETITION_PENALTY`.

OpenAI-compatible and Google providers accept ``frequency_penalty`` /
``presence_penalty``. Anthropic has no penalty fields and is deliberately absent
from every table. Ollama additionally uses its native ``repeat_penalty``.
OpenAI's reasoning models (o-series / gpt-5) also get nothing: their API
rejects both penalty parameters.
"""

from __future__ import annotations

import math
import os

from loguru import logger

from config.features import REPETITION_PENALTY

#: OpenAI-compatible providers (both penalty kwargs).
_OPENAI_COMPATIBLE_PROVIDERS = frozenset(
    {
        "openai",
        "openrouter",
        "deepseek",
        "zhipu",
        "zhipuai",
        "moonshot",
        "dashscope",
        "qwen",
        "siliconflow",
        "mistral",
        "mistralai",
        "cohere",
        "xai",
        "fireworks",
        "perplexity",
        "upstage",
        "azure_openai",
        "azure_ai",
        "groq",
        "volcengine",
        "minimax",
        "together",
        "baseten",
        "aihubmix",
        "openai_responses",
        "azure_responses",
        "vllm",
    }
)

#: Google Generative AI / Vertex aliases.
_GOOGLE_PROVIDERS = frozenset(
    {
        "google",
        "google_genai",
        "google-genai",
        "google_vertexai",
        "vertexai",
        "gemini",
    }
)

_OLLAMA_PROVIDERS = frozenset({"ollama"})

#: OpenAI/Google penalty range (the same range the `.models` write validates).
PENALTY_MIN = -2.0
PENALTY_MAX = 2.0


def _family_value(prefix: str, kind: str) -> float | None:
    """Read one per-family override; blank/invalid falls back to the global."""
    raw = os.getenv(f"{prefix}_{kind}", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning("{}: invalid {}={!r}; using the global default", prefix, kind, raw)
        return None
    if not math.isfinite(value) or not (PENALTY_MIN <= value <= PENALTY_MAX):
        logger.warning(
            "{}: {}={!r} is outside [{}, {}]; using the global default",
            prefix,
            kind,
            raw,
            PENALTY_MIN,
            PENALTY_MAX,
        )
        return None
    return value


def _is_reasoning_model(model_name: str | None) -> bool:
    """Whether OpenAI's reasoning models reject the penalty parameters.

    o-series / gpt-5 accept no ``frequency_penalty`` / ``presence_penalty``
    (the request fails with "Unsupported parameter"), so a routed reasoning
    model must get NO field even though its provider is OpenAI-compatible.
    Detection is lazy and fail-open, mirroring ``provider_max_output``.
    """
    if not model_name:
        return False
    try:
        from models.LLMs.reasoning_payload import is_openai_reasoning_model

        return is_openai_reasoning_model(model_name)
    except Exception:  # noqa: BLE001 - detection is an optimisation, not a gate
        return False


def resolve_repetition_penalty_kwargs(
    provider: str | None,
    frequency_penalty: float | None,
    presence_penalty: float | None,
    *,
    repeat_penalty: float | None = None,
    model_name: str | None = None,
) -> dict[str, float]:
    """Return the provider's supported penalty kwargs (empty when unsupported)."""
    normalized = (provider or "").strip().lower()
    kwargs: dict[str, float] = {}

    if normalized in _OPENAI_COMPATIBLE_PROVIDERS or normalized in _GOOGLE_PROVIDERS:
        if _is_reasoning_model(model_name):
            return {}
        if frequency_penalty is not None:
            kwargs["frequency_penalty"] = float(frequency_penalty)
        if presence_penalty is not None:
            kwargs["presence_penalty"] = float(presence_penalty)
    elif normalized in _OLLAMA_PROVIDERS:
        if repeat_penalty is not None:
            kwargs["repeat_penalty"] = float(repeat_penalty)
        if frequency_penalty is not None:
            kwargs["frequency_penalty"] = float(frequency_penalty)
        if presence_penalty is not None:
            kwargs["presence_penalty"] = float(presence_penalty)
    # Anthropic / Bedrock-anthropic / an OpenAI reasoning model / unknown
    # providers: no penalty fields.
    return kwargs


def family_repetition_penalty_kwargs(
    prefix: str,
    provider: str | None,
    model_name: str | None = None,
) -> dict[str, float]:
    """Resolve one family's ``.models`` overrides into provider kwargs.

    ``prefix`` is e.g. ``MAIN_LLM``. An empty override (the shipped default)
    falls back to the global :data:`REPETITION_PENALTY`; an explicit value
    (including 0) wins. The fallback is per-family, so a user can lower one
    family's penalty without touching the main model.
    """
    frequency = _family_value(prefix, "FREQUENCY_PENALTY")
    presence = _family_value(prefix, "PRESENCE_PENALTY")
    if frequency is None:
        frequency = float(REPETITION_PENALTY["frequency_penalty"])
    if presence is None:
        presence = float(REPETITION_PENALTY["presence_penalty"])
    return resolve_repetition_penalty_kwargs(
        provider,
        frequency,
        presence,
        repeat_penalty=float(REPETITION_PENALTY["repeat_penalty"]),
        model_name=model_name,
    )


__all__ = [
    "PENALTY_MAX",
    "PENALTY_MIN",
    "family_repetition_penalty_kwargs",
    "resolve_repetition_penalty_kwargs",
]
