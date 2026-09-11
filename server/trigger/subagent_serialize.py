"""Shared SubagentRunRecord wire serialization (audit 2.1.5).

``server/trigger/http/subagent.py`` and ``server/trigger/ws/subagent_ws.py``
previously carried identical ``_PUBLIC_FIELDS`` tuples and ``_serialize_run``
implementations (kept in sync manually per their docstrings). The canonical
definitions live here; both consumers re-export them under their original
private names.
"""

from config.features import SERVER_HTTP
from typing import Any

# Fields that are safe / useful to surface to the UI. Everything else (paths,
# attachment dirs, internal policy vectors) is omitted from the wire payload.
PUBLIC_FIELDS = SERVER_HTTP["subagent_public_fields"]


def serialize_run(run) -> dict[str, Any]:
    """Convert a SubagentRunRecord into a JSON-serializable dict with only public fields.

    model_dump(..., mode="json") recursively converts nested pydantic models
    (execution / completion / delivery) and enums into plain JSON-safe values
    that can be handed directly to :func:`json.dumps`.
    """
    return run.model_dump(include=set(PUBLIC_FIELDS), mode="json")
