"""Tiered memory manager: coordinates the structured ``facts/`` layer (LT-1).

``facts/`` files are NOT injected into the system prompt; the agent reads and
searches them on demand through the memory tool. ``prompt_builder`` injects a
single listing line (file names + entry counts only).
"""

from pathlib import Path
from typing import Any

from config import FACTS_DIR
from config.features import TIERED_MEMORY
from agent.tools.memory import ENTRY_DELIMITER, MemoryStore

_FACTS_CHAR_LIMIT: int = TIERED_MEMORY["facts_char_limit"]
_FACTS_CATEGORIES: tuple[str, ...] = TIERED_MEMORY["facts_categories"]


class TieredMemoryStore:
    """Manage the structured fact files under ``facts/``."""

    def __init__(self, memory_store: MemoryStore, facts_dir: Path | None = None) -> None:
        self._memory_store = memory_store
        base = FACTS_DIR if facts_dir is None else facts_dir
        self._files: dict[str, Path] = {cat: base / f"{cat}.md" for cat in _FACTS_CATEGORIES}
        base.mkdir(parents=True, exist_ok=True)
        for path in self._files.values():
            path.touch(exist_ok=True)

    def add_fact(self, category: str, fact: str) -> dict[str, Any]:
        """Append ``fact`` to ``facts/<category>.md`` with dedup and truncation."""
        if category not in self._files:
            return {
                "success": False,
                "error": f"Unknown category '{category}'. Use: {', '.join(_FACTS_CATEGORIES)}",
            }

        fact = fact.strip()
        if not fact:
            return {"success": False, "error": "Fact content cannot be empty."}

        path = self._files[category]

        with self._memory_store._file_lock(path):
            raw = path.read_text(encoding="utf-8") if path.exists() else ""
            entries = (
                [entry.strip() for entry in raw.split(ENTRY_DELIMITER) if entry.strip()]
                if raw.strip()
                else []
            )

            if fact in entries:
                return {
                    "success": True,
                    "message": "Fact already exists (no duplicate added).",
                    "category": category,
                    "entry_count": len(entries),
                    "usage": f"{len(raw)}/{_FACTS_CHAR_LIMIT} chars",
                }

            entries.append(fact)
            combined = ENTRY_DELIMITER.join(entries)

            while len(combined) > _FACTS_CHAR_LIMIT and len(entries) > 1:
                entries.pop(0)
                combined = ENTRY_DELIMITER.join(entries)

            path.write_text(combined, encoding="utf-8")

        return {
            "success": True,
            "message": "Fact added.",
            "category": category,
            "entry_count": len(entries),
            "usage": f"{len(combined)}/{_FACTS_CHAR_LIMIT} chars",
        }

    def read_facts(self, category: str | None = None) -> dict[str, str]:
        """Read one category, or every category when ``category`` is ``None``."""
        if category is not None:
            path = self._files.get(category)
            if path is None:
                return {}
            return {category: path.read_text(encoding="utf-8") if path.exists() else ""}

        return {
            cat: (path.read_text(encoding="utf-8") if path.exists() else "")
            for cat, path in self._files.items()
        }

    def search_facts(self, query: str) -> list[dict[str, str]]:
        """Return entries containing ``query`` (case-insensitive substring)."""
        query_lower = query.lower()
        results: list[dict[str, str]] = []
        for cat, path in self._files.items():
            content = path.read_text(encoding="utf-8") if path.exists() else ""
            for entry in content.split(ENTRY_DELIMITER):
                entry = entry.strip()
                if entry and query_lower in entry.lower():
                    results.append({"category": cat, "fact": entry})
        return results

    def get_facts_listing(self) -> str:
        """Summarise non-empty fact files as ``cat (N entries) · ...``."""
        parts: list[str] = []
        for cat, path in self._files.items():
            content = path.read_text(encoding="utf-8") if path.exists() else ""
            if not content.strip():
                continue
            count = len([entry for entry in content.split(ENTRY_DELIMITER) if entry.strip()])
            parts.append(f"{cat} ({count} entries)")
        return " · ".join(parts)


tiered_store: TieredMemoryStore | None = None


def get_tiered_store() -> TieredMemoryStore:
    """Lazily construct the process-wide :class:`TieredMemoryStore`."""
    global tiered_store
    if tiered_store is None:
        from agent.tools.memory import memory_store

        tiered_store = TieredMemoryStore(memory_store)
    return tiered_store
