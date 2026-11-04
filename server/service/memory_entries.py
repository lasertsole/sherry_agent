"""Memory-entry CRUD, stats, search and the legacy full-file compatibility API.

The memory panel talks to per-entry endpoints (add / edit / archive / restore),
while ``GET/PUT /memory`` keeps its old shape for existing clients and scripts:
GET renders the active entries of each target as ``§``-delimited text, PUT
replaces a target's entries with the parsed text.
"""

from __future__ import annotations

from typing import Any

from agent.tools.memory import ENTRY_DELIMITER, memory_store
from config.features import SERVER_HTTP
from context_engine.store.memory_entry_repository import (
    ARCHIVE_REASONS,
    MEMORY_TARGETS,
)
from context_engine.store.memory_migration import split_legacy_memory_content

MEMORY_SYSTEM_FILE_NAMES: list[str] = SERVER_HTTP["memory_system_file_names"]
_MAX_CONTENT_LENGTH: int = SERVER_HTTP["memory_max_content_length"]
_FILE_BY_TARGET: dict[str, str] = {
    "memory": "MEMORY.md",
    "user": "USER.md",
    "facts": "FACTS.md",
}
_TARGET_BY_FILE: dict[str, str] = {name: target for target, name in _FILE_BY_TARGET.items()}
_MEMORY_MAX_CHARS: int = 8_000


def _repo():
    return memory_store.repo


def _require_target(target: str | None) -> str:
    if target not in MEMORY_TARGETS:
        raise ValueError(f"Invalid target '{target}'. Use 'memory', 'user' or 'facts'.")
    return str(target)


def _require_entry(entry_id: int) -> dict[str, Any]:
    entry = _repo().get_entry(entry_id)
    if entry is None:
        raise LookupError(f"Memory entry {entry_id} not found")
    return entry


def _scan(content: str) -> str:
    from agent.tools.memory import _scan_memory_content

    content = content.strip()
    if not content:
        raise ValueError("Content cannot be empty.")
    if len(content) > _MAX_CONTENT_LENGTH:
        raise ValueError(f"Content too long (max {_MAX_CONTENT_LENGTH} chars).")
    scan_error = _scan_memory_content(content)
    if scan_error:
        raise ValueError(scan_error)
    return content


def _joined_length(target: str, contents: list[str]) -> int:
    del target
    return len(ENTRY_DELIMITER.join(contents))


def _limit_for(target: str) -> int:
    return memory_store._char_limit(target)


def _active_contents(target: str) -> list[str]:
    return [entry["content"] for entry in _repo().active_entries_ordered(target)]


def _check_capacity(target: str, contents: list[str]) -> None:
    limit = min(_limit_for(target), _MEMORY_MAX_CHARS)
    current = _joined_length(target, contents)
    if current > limit:
        raise ValueError(
            f"Memory at {current:,}/{limit:,} chars. Shorten the entry or remove others first."
        )


def list_memory_entries(
    target: str | None = None,
    *,
    include_archived: bool = False,
    sort: str = "created",
    order: str = "desc",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    if target is not None:
        _require_target(target)
    safe_limit = max(1, min(int(limit), 500))
    safe_offset = max(0, int(offset))
    entries = _repo().list_entries(
        target,
        include_archived=include_archived,
        sort=sort,
        order=order,
        limit=safe_limit,
        offset=safe_offset,
    )
    total = _repo().count(target, include_archived=include_archived)
    return {
        "entries": [_public(entry) for entry in entries],
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
        "target": target,
    }


def get_memory_entry(entry_id: int) -> dict[str, Any]:
    return _public(_require_entry(entry_id))


def create_memory_entry(
    target: str | None,
    content: str,
    *,
    entry_type: str | None = None,
    origin_session: str | None = None,
) -> dict[str, Any]:
    target = _require_target(target)
    content = _scan(content)
    contents = _active_contents(target)
    if content in contents:
        raise ValueError("An identical entry already exists.")
    _check_capacity(target, contents + [content])
    entry = _repo().add_entry(
        target,
        content,
        entry_type=entry_type or memory_store._infer_entry_type(target, content),
        origin_session=origin_session,
    )
    memory_store.refresh_live()
    return _public(entry)


def update_memory_entry(
    entry_id: int,
    *,
    content: str | None = None,
    entry_type: str | None = None,
) -> dict[str, Any]:
    current = _require_entry(entry_id)
    if content is not None:
        content = _scan(content)
        contents = [
            content if int(entry["id"]) == entry_id else entry["content"]
            for entry in _repo().active_entries_ordered(str(current["target"]))
        ]
        _check_capacity(str(current["target"]), contents)
    updated = _repo().update_entry(entry_id, content=content, entry_type=entry_type)
    if updated is None:
        raise LookupError(f"Memory entry {entry_id} not found")
    memory_store.refresh_live()
    return _public(updated)


def archive_memory_entry(entry_id: int, reason: str = "manual") -> dict[str, Any]:
    if reason not in ARCHIVE_REASONS:
        raise ValueError(f"Invalid archive reason: {reason!r}")
    _require_entry(entry_id)
    entry = _repo().archive_entry(entry_id, reason)
    memory_store.refresh_live()
    return _public(entry) if entry is not None else {}


def restore_memory_entry(entry_id: int) -> dict[str, Any]:
    current = _require_entry(entry_id)
    if not current.get("archived"):
        return _public(current)
    contents = _active_contents(str(current["target"]))
    _check_capacity(str(current["target"]), contents + [str(current["content"])])
    entry = _repo().restore_entry(entry_id)
    memory_store.refresh_live()
    return _public(entry) if entry is not None else {}


def memory_stats() -> dict[str, Any]:
    stats = _repo().stats()
    targets: dict[str, Any] = {}
    for target in MEMORY_TARGETS:
        row = stats.get(target, {"count": 0, "chars": 0, "avg_usage": 0.0, "archived": 0})
        limit = _limit_for(target)
        targets[target] = {
            **row,
            "limit": limit,
            "percent": min(100, int((row["chars"] / limit) * 100)) if limit > 0 else 0,
            "file": _FILE_BY_TARGET[target],
        }
    return {
        "targets": targets,
        "settings": memory_store.effective_limits(),
        "total_entries": sum(row["count"] for row in targets.values()),
        "total_chars": sum(row["chars"] for row in targets.values()),
    }


def search_memory(
    query: str,
    *,
    target: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    if target is not None:
        _require_target(target)
    needle = (query or "").strip().lower()
    if not needle:
        return {"entries": [], "total": 0, "query": query}
    safe_limit = max(1, min(int(limit), 200))
    entries = _repo().list_entries(
        target,
        include_archived=False,
        sort="last_used",
        order="desc",
        limit=None,
    )
    matches = [entry for entry in entries if needle in entry["content"].lower()]
    return {
        "entries": [_public(entry) for entry in matches[:safe_limit]],
        "total": len(matches),
        "query": query,
    }


def consolidate_memory() -> dict[str, Any]:
    """Run the usage-based aging pass (the manual 立即整理 button)."""
    archived = _repo().prune_unused(int(memory_store.max_unused_days))
    memory_store.refresh_live()
    return {
        "archived": archived,
        "max_unused_days": int(memory_store.max_unused_days),
        "stats": memory_stats(),
    }


def read_memory_settings() -> dict[str, Any]:
    return memory_store.effective_limits()


def write_memory_settings(values: dict[str, Any]) -> dict[str, Any]:
    return memory_store.update_settings(values)


# -- legacy full-file compatibility ---------------------------------------


def read_memory_files() -> dict[str, str]:
    """Render active entries as the old ``§``-delimited file bodies."""
    result: dict[str, str] = {}
    for target in MEMORY_TARGETS:
        contents = _active_contents(target)
        if contents:
            result[_FILE_BY_TARGET[target]] = ENTRY_DELIMITER.join(contents)
    return result


def write_memory_files(file_to_content: dict[str, str]) -> None:
    """Replace each provided target's entries with the parsed file body."""
    if not isinstance(file_to_content, dict):
        raise ValueError("file_to_content must be an object mapping file name to content")
    for file_name, raw in file_to_content.items():
        if file_name not in _TARGET_BY_FILE:
            raise ValueError(f"Invalid memory file name: {file_name}")
        if not isinstance(raw, str):
            raise ValueError(f"Invalid content type for memory file: {file_name}")
        if not raw.strip():
            raise ValueError(f"Content is empty for memory file: {file_name}")
        if len(raw) > _MAX_CONTENT_LENGTH:
            raise ValueError(f"Content too long for memory file: {file_name}")

    # Validate every provided body BEFORE any target is rewritten, so one bad
    # file cannot leave the store half-replaced.
    parsed_by_target: dict[str, list[str]] = {}
    for file_name, raw in file_to_content.items():
        target = _TARGET_BY_FILE[file_name]
        raw_entries = split_legacy_memory_content(raw)
        entries = [_scan(entry) for entry in raw_entries]
        limit = min(_limit_for(target), _MEMORY_MAX_CHARS)
        if _joined_length(target, entries) > limit:
            raise ValueError(f"Content too long for memory file: {file_name}")
        parsed_by_target[target] = entries

    with _repo().transaction():
        for target, entries in parsed_by_target.items():
            for entry in _repo().active_entries_ordered(target):
                _repo().archive_entry(int(entry["id"]), "superseded")
            for content in entries:
                _repo().add_entry(target, content)
    memory_store.refresh_live()


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": entry["id"],
        "target": entry["target"],
        "content": entry["content"],
        "entry_type": entry.get("entry_type"),
        "origin_session": entry.get("origin_session"),
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
        "last_used": entry.get("last_used"),
        "usage_count": entry.get("usage_count", 0),
        "char_count": entry.get("char_count", len(entry.get("content", ""))),
        "archived": bool(entry.get("archived")),
        "archived_at": entry.get("archived_at"),
        "archived_reason": entry.get("archived_reason"),
    }
