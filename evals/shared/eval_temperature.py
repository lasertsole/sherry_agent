"""Deterministic decoding for coding-benchmark eval runs.

Eval arms must not inherit the shipped persona's temperature 0.8: two runs of
the same instance would sample different patches, making cross-suite
comparisons meaningless. The override is installed at the
``server.service.messages.built_agent`` seam, so the product default stays
untouched and every eval turn (including a HITL resume) rebuilds through it.

Shared by all coding benchmark suites; extracted from
``evals/coding_capability/suite.py`` so the behavior is identical everywhere.
"""

from __future__ import annotations

from typing import Any

#: Eval runs pin the main model to deterministic decoding. The shipped agent
#: default is temperature 0.8 (persona behaviour).
EVAL_TEMPERATURE = 0.0

_PIN_MARKER = "_sherry_eval_temperature_pin"


def pin_eval_temperature(temperature: float = EVAL_TEMPERATURE) -> None:
    """Force ``server.service.messages.built_agent`` to build at ``temperature``.

    Patched at the module seam rather than changing the product default: the
    server keeps its persona temperature, while every eval turn (including a
    HITL resume) rebuilds the graph through this wrapper. Idempotent so a
    second suite in the same interpreter cannot stack wrappers.
    """
    import server.service.messages as messages

    original = messages.built_agent
    if getattr(original, _PIN_MARKER, False):
        return

    async def _pinned(*args: Any, **kwargs: Any) -> Any:
        kwargs["temperature"] = temperature
        return await original(*args, **kwargs)

    setattr(_pinned, _PIN_MARKER, True)
    messages.built_agent = _pinned
