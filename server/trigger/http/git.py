"""Git graph route (``GET /git/graph``) — the left sidebar's read-only history panel.

Boundary: the repository is the SESSION's project directory, the same root
``GET /project/tree`` serves, and every git call is read-only (``rev-parse`` /
``log`` / ``status``). A directory that is not a repository answers
``available: false`` with a reason (`not-a-repository` / `git-unavailable`), so
the client renders its own empty state instead of an error toast.

Query: ``session_id`` (required), ``limit`` (commits per page, clamped),
``skip`` (paging offset). Answers
``{root, source, available, reason, branch, detached, dirty, commits, has_more}``
with one ``{hash, short, parents, author, date, refs, subject}`` per commit.
"""

import asyncio

from loguru import logger

from config.features import GIT_GRAPH
from server.service.git_graph_service import read_graph
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, ok, query_int


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
    return ok(
        {
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
    )
