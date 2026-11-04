"""Per-entry long-term memory API for the memory panel.

The old ``GET/PUT /memory`` routes stay (``server/trigger/http/memory.py``) for
compatibility; these routes are the CRUD surface the panel uses.
"""

from loguru import logger

from context_engine.store.memory_entry_repository import ARCHIVE_REASONS
from server.service.memory_entries import (
    archive_memory_entry,
    consolidate_memory,
    create_memory_entry,
    get_memory_entry,
    list_memory_entries,
    memory_stats,
    read_memory_settings,
    restore_memory_entry,
    search_memory,
    update_memory_entry,
    write_memory_settings,
)
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, not_found, ok, query_int, read_body


def _entry_id(path_params) -> int:
    return int(path_params["entry_id"])


@app.get("/memory/entries/stats")
async def memory_entries_stats_handler(request):
    return ok(memory_stats())


@app.get("/memory/entries")
async def memory_entries_list_handler(request):
    query = request.query_params or {}
    # Robyn's QueryParams.get REQUIRES its default argument (the documented
    # gotcha); omitting it raises before the handler can answer.
    target = query.get("target", "") or None
    include_archived = str(query.get("include_archived", "") or "").lower() in {
        "1",
        "true",
        "yes",
    }
    sort = query.get("sort", "created") or "created"
    order = query.get("order", "desc") or "desc"
    try:
        return ok(
            list_memory_entries(
                target,
                include_archived=include_archived,
                sort=sort,
                order=order,
                limit=query_int(query, "limit", 100, minimum=1, maximum=500),
                offset=query_int(query, "offset", 0, minimum=0),
            )
        )
    except ValueError as exc:
        return bad_request(str(exc))


@app.post("/memory/entries")
async def memory_entries_create_handler(request):
    body = read_body(request) or {}
    try:
        entry = create_memory_entry(
            body.get("target"),
            str(body.get("content") or ""),
            entry_type=body.get("entry_type") or None,
            origin_session=body.get("origin_session") or None,
        )
        return ok(entry)
    except ValueError as exc:
        return bad_request(str(exc))


@app.get("/memory/entries/:entry_id")
async def memory_entries_get_handler(request, path_params):
    try:
        return ok(get_memory_entry(_entry_id(path_params)))
    except LookupError as exc:
        return not_found(str(exc))
    except ValueError as exc:
        return bad_request(str(exc))


@app.put("/memory/entries/:entry_id")
async def memory_entries_update_handler(request, path_params):
    body = read_body(request) or {}
    try:
        entry = update_memory_entry(
            _entry_id(path_params),
            content=body.get("content"),
            entry_type=body.get("entry_type"),
        )
        return ok(entry)
    except LookupError as exc:
        return not_found(str(exc))
    except ValueError as exc:
        return bad_request(str(exc))


@app.delete("/memory/entries/:entry_id")
async def memory_entries_archive_handler(request, path_params):
    body = read_body(request) or {}
    reason = str(body.get("reason") or "manual")
    if reason not in ARCHIVE_REASONS:
        return bad_request(f"Invalid archive reason: {reason!r}")
    try:
        return ok(archive_memory_entry(_entry_id(path_params), reason))
    except LookupError as exc:
        return not_found(str(exc))


@app.post("/memory/entries/:entry_id/restore")
async def memory_entries_restore_handler(request, path_params):
    try:
        return ok(restore_memory_entry(_entry_id(path_params)))
    except LookupError as exc:
        return not_found(str(exc))
    except ValueError as exc:
        return bad_request(str(exc))


@app.post("/memory/search")
async def memory_search_handler(request):
    body = read_body(request) or {}
    try:
        return ok(
            search_memory(
                str(body.get("query") or ""),
                target=body.get("target") or None,
                limit=int(body.get("limit") or 50),
            )
        )
    except (TypeError, ValueError) as exc:
        return bad_request(str(exc))


@app.post("/memory/consolidate")
async def memory_consolidate_handler(request):
    logger.info("memory consolidate requested")
    return ok(consolidate_memory())


@app.get("/memory/settings")
async def memory_settings_read_handler(request):
    return ok(read_memory_settings())


@app.put("/memory/settings")
async def memory_settings_write_handler(request):
    body = read_body(request) or {}
    try:
        return ok(write_memory_settings(body))
    except ValueError as exc:
        return bad_request(str(exc))
