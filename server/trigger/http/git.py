"""Git graph routes — the left sidebar's history panel.

Boundary: the repository is the SESSION's project directory, the same root
``GET /project/tree`` serves. ``GET /git/graph`` is read-only (``rev-parse`` /
``log`` / ``status``) and answers ``available: false`` with a reason
(`not-a-repository` / `git-unavailable`) for a directory that is not a usable
repository, so the client renders its own empty state instead of an error.

The two write actions are the panel's context menu: ``POST /git/reset`` moves the
current branch (soft / mixed / hard) and ``POST /git/checkout`` switches branches.
Both validate the target with ``git rev-parse --verify`` before git runs, take no
flags from the caller, and surface git's own stderr as the refusal message.

``GET /git/graph``: ``session_id`` (required), ``limit`` (commits per page,
clamped), ``skip`` (paging offset). Every route answers the same page payload —
``{root, source, available, reason, branch, detached, dirty, commits, has_more}``
with one ``{hash, short, parents, author, date, refs, subject}`` per commit.
"""

import asyncio

from loguru import logger

from config.features import GIT_GRAPH
from server.service.git_graph_service import (
    GitActionError,
    checkout_ref,
    read_graph,
    reset_to,
)
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, ok, query_int, read_body, to_text_response


def _page_payload(session_id: str, page) -> dict:
    """The one JSON shape both the read route and the write actions answer with."""
    return {
        "success": True,
        "session_id": session_id,
        "root": page.root,
        "source": page.source,
        "available": page.available,
        "reason": page.reason,
        "branch": page.branch,
        "detached": page.detached,
        "dirty": page.dirty,
        "dirty_capped": page.dirty_capped,
        "commits": page.commits,
        "has_more": page.has_more,
    }


def _refusal(exc: GitActionError):
    """Map a git-action refusal onto the response contract (never a raw exception)."""
    if exc.status == 400:
        return bad_request(exc.reason)
    return to_text_response(exc.status, {"success": False, "reason": exc.reason})


@app.get("/git/graph")
async def git_graph_handler(request):
    """One page of the session project's git history."""
    query = request.query_params or {}
    session_id = query.get("session_id", "") or ""
    if not session_id:
        return bad_request("session_id is required")
    # `min`/`max` come from the config, so a caller can ask for a smaller page but
    # never for an unbounded one.
    limit = query_int(
        query,
        "limit",
        GIT_GRAPH["page_size"],
        minimum=1,
        maximum=GIT_GRAPH["max_page_size"],
    )
    skip = query_int(query, "skip", 0, minimum=0)
    page = await asyncio.to_thread(read_graph, session_id, limit, skip)
    if not page.available:
        logger.info(
            "git graph unavailable: session={} root={} reason={}",
            session_id,
            page.root,
            page.reason,
        )
    return ok(_page_payload(session_id, page))


@app.post("/git/reset")
async def git_reset_handler(request):
    """Move the current branch to a commit (``{"session_id", "hash", "mode"}``).

    ``mode`` is soft / mixed / hard; ``hard`` discards uncommitted changes and the
    panel confirms it first. Answers the refreshed page on success.
    """
    body = read_body(request) or {}
    session_id = str(body.get("session_id") or "")
    target = str(body.get("hash") or "")
    mode = str(body.get("mode") or "soft")
    if not session_id:
        return bad_request("session_id is required")
    try:
        page = await asyncio.to_thread(reset_to, session_id, target, mode)
    except GitActionError as exc:
        logger.info(
            "git reset refused: session={} target={} reason={}",
            session_id,
            target[:12],
            exc.reason,
        )
        return _refusal(exc)
    return ok(_page_payload(session_id, page))


@app.post("/git/checkout")
async def git_checkout_handler(request):
    """Check out a branch or ref (``{"session_id", "ref"}``). Answers the refreshed page."""
    body = read_body(request) or {}
    session_id = str(body.get("session_id") or "")
    ref = str(body.get("ref") or "")
    if not session_id:
        return bad_request("session_id is required")
    try:
        page = await asyncio.to_thread(checkout_ref, session_id, ref)
    except GitActionError as exc:
        logger.info(
            "git checkout refused: session={} ref={} reason={}",
            session_id,
            ref[:40],
            exc.reason,
        )
        return _refusal(exc)
    return ok(_page_payload(session_id, page))
