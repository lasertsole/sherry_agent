"""Memory-entry CRUD, stats, search and the legacy full-file compatibility API.

The memory panel talks to per-entry endpoints (add / edit / archive / restore),
while ``GET/PUT /memory`` keeps its old shape for existing clients and scripts:
GET renders the active entries of each target as ``§``-delimited text, PUT
replaces a target's entries with the parsed text.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from enum import StrEnum
from typing import Any

from loguru import logger

from agent.tools.memory import ENTRY_DELIMITER, memory_store
from config.features import MEMORY_TOOL, SERVER_HTTP
from context_engine.store.memory_entry_repository import (
    ARCHIVE_REASONS,
    MEMORY_TARGETS,
)
from context_engine.store.memory_migration import split_legacy_memory_content

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
    max_entries = int(MEMORY_TOOL["max_entries_per_target"])
    if current > limit:
        raise ValueError(
            f"Memory at {current:,}/{limit:,} chars. Shorten the entry or remove others first."
        )
    if len(contents) > max_entries:
        raise ValueError(
            f"Memory holds {len(contents):,} active entries (limit {max_entries:,}). "
            "Remove or merge entries first."
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
    session_id: str | None = None,
) -> dict[str, Any]:
    if target is not None:
        _require_target(target)
    needle = (query or "").strip().lower()
    if not needle:
        return {"entries": [], "total": 0, "query": query}
    safe_limit = max(1, min(int(limit), 200))
    if memory_store.semantic_recall_enabled:
        matches = memory_store.recall(
            query,
            target,
            limit=min(safe_limit, 50),
            session_id=session_id,
        )
        rows: list[dict[str, Any]] = []
        for hit in matches:
            entry = _repo().get_entry(int(hit["id"]))
            if entry is None:
                continue
            rows.append({**_public(entry), "score": hit.get("score"), "stale": hit.get("stale")})
        return {
            "entries": rows,
            "total": len(rows),
            "query": query,
            "mode": "semantic",
        }
    entries = _repo().list_entries(
        target,
        include_archived=False,
        sort="last_used",
        order="desc",
        limit=None,
    )
    substring_matches = [entry for entry in entries if needle in entry["content"].lower()]
    return {
        "entries": [_public(entry) for entry in substring_matches[:safe_limit]],
        "total": len(substring_matches),
        "query": query,
        "mode": "substring",
    }


def consolidate_memory() -> dict[str, Any]:
    """Run ONLY the usage-based aging pass (kept for compatibility/tests)."""
    archived = _repo().prune_unused(int(memory_store.max_unused_days))
    memory_store.refresh_live()
    return {
        "archived": archived,
        "max_unused_days": int(memory_store.max_unused_days),
        "stats": memory_stats(),
    }


class ConsolidationTaskStatus(StrEnum):
    """The process-local state of one manual consolidation task (audit L10)."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


#: ``{task_id: record}`` for the async manual-consolidation button. Process-local
#: on purpose: a restart abandons an in-flight pass, and the panel simply starts
#: a new one.
_CONSOLIDATION_TASKS: dict[str, dict[str, Any]] = {}
_CONSOLIDATION_KEEP = 50


def _prune_consolidation_tasks() -> None:
    finished = [
        key
        for key, value in _CONSOLIDATION_TASKS.items()
        if value["status"] != ConsolidationTaskStatus.RUNNING.value
    ]
    for key in finished[: max(0, len(finished) - _CONSOLIDATION_KEEP)]:
        _CONSOLIDATION_TASKS.pop(key, None)


def start_memory_consolidation(session_id: str) -> dict[str, Any]:
    """Start the async aging + LLM consolidation pass; returns its task id.

    The handler must run on the event loop (Robyn's async handlers do); the
    worker itself is a detached task so a long model pass never blocks the
    HTTP response.
    """
    if not session_id:
        raise ValueError("session_id is required for memory consolidation")
    loop = asyncio.get_running_loop()
    task_id = uuid.uuid4().hex
    record: dict[str, Any] = {
        "task_id": task_id,
        "session_id": session_id,
        "status": ConsolidationTaskStatus.RUNNING.value,
        "started_at": time.time(),
        "finished_at": None,
        "result": None,
        "error": None,
    }
    _CONSOLIDATION_TASKS[task_id] = record
    _prune_consolidation_tasks()

    async def _runner() -> None:
        from agent.middlewares.summarization.background_reviews import run_memory_consolidation

        try:
            result = await run_memory_consolidation(session_id)
            record["status"] = str(result.get("status") or "completed")
            record["result"] = result
        except Exception as exc:  # noqa: BLE001 - report on the task record
            logger.exception("memory consolidation task {} failed", task_id)
            record["status"] = ConsolidationTaskStatus.FAILED.value
            record["error"] = str(exc)
        finally:
            record["finished_at"] = time.time()

    loop.create_task(_runner())
    return {"task_id": task_id, "status": ConsolidationTaskStatus.RUNNING.value}


def consolidation_status(task_id: str) -> dict[str, Any] | None:
    """Return one task's record (a copy), or ``None`` when unknown."""
    record = _CONSOLIDATION_TASKS.get(task_id)
    return dict(record) if record is not None else None


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
        if len(entries) > int(MEMORY_TOOL["max_entries_per_target"]):
            raise ValueError(f"Too many entries for memory file: {file_name}")
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
