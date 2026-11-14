"""Factories for the optionally-independent internal LLM families.

``TASKFLOW_LLM_*`` (TaskFlow decision calls), ``COMPRESSION_LLM_*`` (the
summarization middleware's own model) and the per-role subagent families
(``EXECUTOR_LLM_*`` / ``RESEARCHER_LLM_*`` / ``REVIEWER_LLM_*`` /
``LIBRARIAN_LLM_*``) follow the same three-way rule:

1. ``<PREFIX>_MODEL_LOCAL=true`` → the fallback family's local branch. Decision
   A of the plan: local mode REUSES an already-downloaded GGUF, so enabling a
   family costs no second download and the desktop bundle does not grow.
2. ``<PREFIX>_PROVIDER`` or ``<PREFIX>_API_BASE`` set → a remote client built
   exactly like the auxiliary one (same retry/timeout/max-output plumbing).
3. Neither set → the fallback family — ``AUXILIARY_LLM`` by default, ``main``
   for the per-role families (a role's spawn resolver only reaches its builder
   when the family is configured, so an empty role family keeps the role's own
   model tier).

The remote branch is fail-open at BUILD time: a provider name that
``init_chat_model`` cannot resolve (a missing integration package, an unknown
provider) falls back to the chosen family with a warning instead of taking the
TaskFlow/compression/child surface down. A wrong endpoint that only fails at
invoke time is NOT hidden — the call sites already own their fail-open
reporting.
"""

from __future__ import annotations

import os
from typing import Any, Literal

from loguru import logger

from config.features import LLM_CLIENT_DEFAULTS
from config.path import load_env
from models.LLMs.provider_max_output import (
    configured_max_output,
    resolve_max_output_kwargs,
)
from models.LLMs.provider_repetition_penalty import family_repetition_penalty_kwargs
from models.LLMs.reasoning_normalizer import NormalizingChatModel
from models.env_builder import ModelEnvBuilder, read_env

__all__ = ["build_family_llm", "family_configured"]

#: Read .models / .env before the first call site builds a client.
load_env()

#: Which family an unconfigured (or locally-configured) family falls back to.
FallbackFamily = Literal["auxiliary", "main"]


def _build_fallback(fallback: FallbackFamily, temperature: float | None) -> Any:
    """The fallback model, resolved through the ``models`` package at CALL time.

    Going through the package attribute (instead of binding the builder at
    import) keeps the existing test seam — tests patch
    ``models.build_auxiliary_llm`` / ``models.build_main_llm`` to inject a stub —
    working unchanged, and keeps the lazy-export contract of
    ``models/__init__.py``.
    """
    import models

    if fallback == "main":
        return models.build_main_llm(temperature=temperature)
    return models.build_auxiliary_llm(temperature=temperature)


def _family_is_configured(prefix: str) -> tuple[bool, bool]:
    """``(remote_configured, local)`` for one family prefix."""
    provider = read_env(f"{prefix}_PROVIDER")
    api_base = read_env(f"{prefix}_API_BASE")
    is_local = os.getenv(f"{prefix}_MODEL_LOCAL", "").strip().lower() == "true"
    return bool(provider or api_base), is_local


def family_configured(prefix: str) -> bool:
    """Whether one family is deliberately configured (remote OR local).

    The spawn resolver uses this to choose between a per-role family and the
    role's own model tier: an unconfigured family must keep the existing tier
    instead of silently redirecting the child.
    """
    remote_configured, is_local = _family_is_configured(prefix)
    return remote_configured or is_local


def build_family_llm(
    prefix: str,
    temperature: float | None = None,
    *,
    fallback: FallbackFamily = "auxiliary",
) -> Any:
    """Build the model for one family (``TASKFLOW_LLM`` / ``EXECUTOR_LLM`` / …).

    A fresh instance per call: the same event-loop binding rule as
    :func:`build_auxiliary_llm` (a cached client deadlocks across subagent
    threads), and the same temperature default (0 — these are deterministic
    decision / summary calls unless a caller binds another value).

    ``fallback`` names the family an unconfigured or local family reuses:
    ``"auxiliary"`` (the historical default) or ``"main"`` (per-role children).
    """
    remote_configured, is_local = _family_is_configured(prefix)

    # Local mode wins over provider config, and an unconfigured family follows
    # its declared fallback family.
    if is_local or not remote_configured:
        return _build_fallback(fallback, temperature)

    from langchain.chat_models import init_chat_model

    raw_max = os.getenv(f"{prefix}_MAX_TOKEN", "").strip()
    max_tokens = int(raw_max) if raw_max else LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]

    try:
        model_config: dict[str, Any] = ModelEnvBuilder(
            {
                "model_provider": f"{prefix}_PROVIDER",
                "model": f"{prefix}_API_NAME",
                "api_key": f"{prefix}_API_KEY",
                "base_url": f"{prefix}_API_BASE",
            }
        ).build(
            {
                "temperature": temperature if temperature is not None else 0,
                "max_retries": LLM_CLIENT_DEFAULTS["aux_max_retries"],
                "timeout": LLM_CLIENT_DEFAULTS["aux_timeout"],
                "profile": {"max_input_tokens": max_tokens},
                **resolve_max_output_kwargs(
                    os.getenv(f"{prefix}_PROVIDER"),
                    os.getenv(f"{prefix}_API_NAME"),
                    configured_max_output(f"{prefix}_MAX_OUTPUT_TOKEN"),
                ),
                **family_repetition_penalty_kwargs(
                    prefix,
                    os.getenv(f"{prefix}_PROVIDER"),
                    os.getenv(f"{prefix}_API_NAME"),
                ),
            }
        )
        from models.LLMs.anthropic_cache import maybe_wrap_anthropic_cache

        inner = maybe_wrap_anthropic_cache(
            init_chat_model(**model_config), os.getenv(f"{prefix}_PROVIDER")
        )
        return NormalizingChatModel(inner=inner)
    except Exception as exc:  # noqa: BLE001 - build-time fail-open (plan §3.5)
        logger.warning(
            "{}: configured model could not be built ({}); falling back to the {} model",
            prefix,
            exc,
            fallback,
        )
        return _build_fallback(fallback, temperature)
