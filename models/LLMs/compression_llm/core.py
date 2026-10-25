"""Compression LLM (``COMPRESSION_LLM_*``).

Serves the summarization middleware's own model: the LLM summary
(``_create_summary`` / ``_acreate_summary``). It does NOT serve memory review
(that is the MAIN model, cache-warm by design) or plan extraction (that stays
on the auxiliary model). An unconfigured family falls back to the auxiliary
model. See :mod:`models.LLMs.family_llm` for the three-way rule.
"""

from typing import Any

from models.LLMs.family_llm import build_family_llm

__all__ = ["build_compression_llm"]


def build_compression_llm(temperature: float | None = None) -> Any:
    """Create a fresh compression model bound to the current event loop."""
    return build_family_llm("COMPRESSION_LLM", temperature)
