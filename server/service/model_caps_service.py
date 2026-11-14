"""Read and clear the persisted subagent structured-output degradations.

The store itself is ``agent.tools.subagent.model_caps``: one label per
``(provider, model)`` persisted in ``subagent_model_caps.json``. This service is
the server-side seam so the HTTP trigger never touches the file format; the
model-caps panel reads it and can clear one entry (retry immediately) or all.
"""

from __future__ import annotations

from typing import Any

from agent.tools.subagent import model_caps

__all__ = ["clear_all_model_caps", "clear_model_cap", "read_model_caps"]


def read_model_caps() -> list[dict[str, Any]]:
    """Every persisted degradation plus the level the next spawn would use."""
    return model_caps.list_entries()


def clear_model_cap(provider: str, model: str) -> bool:
    """Forget one entry; returns whether it existed."""
    return model_caps.clear_entry(provider, model)


def clear_all_model_caps() -> int:
    """Forget every entry; returns how many were removed."""
    return model_caps.clear_all()
