"""LLM types shared across packages (agent / models)."""

from typing import Any
from dataclasses import dataclass


@dataclass
class FallbackCandidate:
    """One fallback model candidate for ``LLMRetryMiddleware``.

    Built by ``models.LLMs.main_llm.build_fallback_chain`` from the
    ``FALLBACK_LLM_{i}_*`` env vars; consumed by the retry middleware's
    sticky per-session fallback index.
    """

    provider: str
    model_name: str
    model: Any
