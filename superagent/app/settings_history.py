"""Settings change history (prototype ``src/app/settings-history.ts``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HistoryEntry:
    """One saved settings revision."""

    block: str
    revision: int
    value: Any
    at: str


class SettingsHistory:
    """Append-only history with a bounded tail."""

    def __init__(self, limit: int = 200) -> None:
        self.limit = limit
        self._entries: list[HistoryEntry] = []

    def append(self, block: str, revision: int, value: Any, at: str) -> None:
        """Record a saved revision."""
        self._entries.append(HistoryEntry(block, revision, value, at))
        if len(self._entries) > self.limit:
            self._entries = self._entries[-self.limit :]

    def for_block(self, block: str) -> list[HistoryEntry]:
        """Return the history of one block."""
        return [entry for entry in self._entries if entry.block == block]


__all__ = ["HistoryEntry", "SettingsHistory"]
