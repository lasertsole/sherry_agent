"""Read-only project file browser routes (``GET /project/tree`` + ``/project/file``).

The tree is lazy: one directory level per call (a recursive tree with inline
contents — the skills endpoint's shape — cannot serve a real project). Both
routes resolve inside the session's project directory only; auth, CORS and CSRF
are inherited from the global middleware, and the blocking filesystem work runs
in a worker thread.
"""

import asyncio
from urllib.parse import unquote

from loguru import logger

from server.service.project_files_service import (
    FileBrowserError,
    list_level,
    read_file,
)
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, not_found, ok, to_text_response


def _refusal(exc: FileBrowserError):
    """Map a browser refusal onto the response contract (never a raw exception)."""
    if exc.status == 404:
        return not_found(exc.reason)
    if exc.status in (413, 415):
        return to_text_response(exc.status, {"success": False, "reason": exc.reason})
    return bad_request(exc.reason)


@app.get("/project/tree")
async def project_tree_handler(request):
    """List one level of the session's project tree.

    Query: ``session_id`` (required), ``path`` (relative subdirectory, empty =
    the root). Answers ``{root, path, entries, truncated, total}``; entries carry
    ``{name, type, size?}`` with directories first.
    """
    query = request.query_params or {}
    session_id = query.get("session_id", "") or ""
    if not session_id:
        return bad_request("session_id is required")
    # Query values arrive percent-encoded (`src%2Fapp.py`); the shared logs
    # route decodes the same way before touching the filesystem.
    rel_path = unquote(query.get("path", "") or "")
    try:
        level = await asyncio.to_thread(list_level, session_id, rel_path)
    except FileBrowserError as exc:
        logger.info(
            "project tree refused: session={} path={} reason={}", session_id, rel_path, exc.reason
        )
        return _refusal(exc)
    return ok(
        {
            "success": True,
            "session_id": session_id,
            "root": level.root,
            "path": level.path,
            "entries": level.entries,
            "truncated": level.truncated,
            "total": level.total,
        }
    )


@app.get("/project/file")
async def project_file_handler(request):
    """Read one file from the session's project tree.

    Query: ``session_id`` (required), ``path`` (relative file path, required).
    Answers ``{path, encoding, content, size, truncated}``; 413 above the
    configured size cap, 415 for a file that is not UTF-8 text.
    """
    query = request.query_params or {}
    session_id = query.get("session_id", "") or ""
    if not session_id:
        return bad_request("session_id is required")
    rel_path = unquote(query.get("path", "") or "")
    if not rel_path:
        return bad_request("path is required")
    try:
        content = await asyncio.to_thread(read_file, session_id, rel_path)
    except FileBrowserError as exc:
        logger.info(
            "project file refused: session={} path={} reason={}", session_id, rel_path, exc.reason
        )
        return _refusal(exc)
    return ok(
        {
            "success": True,
            "session_id": session_id,
            "path": content.path,
            "encoding": "utf-8",
            "content": content.content,
            "size": content.size,
            "truncated": content.truncated,
        }
    )
