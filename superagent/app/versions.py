"""Revisioned settings blocks (prototype ``src/app/versions.ts``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Versioned:
    """A value plus its monotonically increasing revision."""

    value: Any
    revision: int = 0


class Versions:
    """In-memory snapshot of all revisioned blocks."""

    def __init__(self) -> None:
        self.blocks: dict[str, Versioned] = {}

    def get(self, block: str) -> Versioned:
        """Return a block, creating an empty one when missing."""
        return self.blocks.setdefault(block, Versioned(value={}))

    def save(self, block: str, value: Any) -> Versioned:
        """Save a block and bump its revision."""
        current = self.get(block)
        saved = Versioned(value=value, revision=current.revision + 1)
        self.blocks[block] = saved
        return saved

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a panel-friendly snapshot."""
        return {
            name: {"value": item.value, "revision": item.revision}
            for name, item in self.blocks.items()
        }


__all__ = ["Versioned", "Versions"]
