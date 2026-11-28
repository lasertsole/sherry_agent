"""Settings views (prototype ``src/app/views/settings.ts``)."""

from __future__ import annotations

from typing import Any

SETTINGS_GROUPS = ("sources", "eval", "executor", "prompts", "deployment")


def page(snapshot: dict[str, dict[str, Any]], prompts: dict[str, Any]) -> dict[str, Any]:
    """Return the settings page data."""
    return {
        "groups": {
            group: snapshot.get(group, {"value": {}, "revision": 0}) for group in SETTINGS_GROUPS
        },
        "prompts": prompts,
        "environment": {"root": "", "variables": []},
    }
