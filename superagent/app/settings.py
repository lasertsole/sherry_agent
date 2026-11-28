"""Volatile settings store (prototype ``src/app/settings.ts``)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from superagent.app.settings_history import SettingsHistory
from superagent.app.versions import Versioned, Versions


class SettingsStore:
    """Single writer of settings blocks with revision checks and history."""

    def __init__(self) -> None:
        self.versions = Versions()
        self.history = SettingsHistory()

    def get(self, block: str) -> Versioned:
        """Return one block."""
        return self.versions.get(block)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return all blocks."""
        return self.versions.snapshot()

    def save(self, block: str, value: Any, revision: int) -> Versioned:
        """Save a block; raise on stale revisions."""
        current = self.get(block)
        if revision != current.revision:
            from superagent.core.errors import UserError

            raise UserError("stale_revision", {"block": block, "revision": revision})
        saved = self.versions.save(block, value)
        self.history.append(block, saved.revision, saved.value, datetime.now(UTC).isoformat())
        return saved


__all__ = ["SettingsStore"]
