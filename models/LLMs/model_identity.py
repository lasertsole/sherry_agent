"""Identify the model a session's thinking request is built for.

The custom thinking mapping is authored for ONE model: a ``wire_format`` /
``level_map`` pair describes how that provider wants its reasoning payload, and
applying it to a different model produces invalid request fields. It is
therefore stamped with this key at write time and only applied while the key
still matches.

The key is neutral (no server/agent imports) so both sides can use it: the
service stamps/reads it, the middleware compares it on the hot path, and tests
can build one from a plain descriptor.
"""

from __future__ import annotations

__all__ = ["main_model_key"]


def main_model_key(override: dict | None = None) -> str:
    """``"<provider>/<model>"`` for the session's effective main model.

    ``override`` is the session's parked/live model descriptor (``None`` = follow
    the environment config, whose identity is read from the main-LLM module).
    Both halves are lowercased/stripped so a cosmetic difference in casing cannot
    make a mapping look stale.
    """
    if isinstance(override, dict) and override.get("model"):
        provider = override.get("provider") or ""
        model = override.get("model") or ""
    else:
        from models.LLMs import main_llm as _main_llm

        provider = _main_llm.model_provider or ""
        model = _main_llm.api_name or ""
    return f"{str(provider).strip().lower()}/{str(model).strip().lower()}"
