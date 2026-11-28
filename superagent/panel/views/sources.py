"""Source status views (prototype ``src/app/views/sources.ts``)."""

from __future__ import annotations

from typing import Any


def source_state(instance: Any, health: dict[str, dict[str, Any]]) -> dict[str, object]:
    """Return one Source status row."""
    problems = health.get(instance.kind, {})
    state = (
        "missing"
        if not instance.enabled
        else "error"
        if problems.get("state") == "error"
        else "ready"
    )
    return {
        "id": instance.id,
        "kind": instance.kind,
        "enabled": instance.enabled,
        "state": state,
        "detail": problems.get("detail", ""),
    }
