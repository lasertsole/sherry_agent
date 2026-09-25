"""Re-entrant event-loop patching for the vendored LightRAG sync shims.

The vendored LightRAG (``graph_rag/vendored_lightrag``) exposes sync wrappers
around its coroutines — ``loop.run_until_complete(self.aquery(...))`` and
friends — so they only work when the ambient loop is re-entrant. That is what
``nest_asyncio`` provides, and it is why the module used to call
``nest_asyncio.apply()`` at import time.

Under uvloop (Robyn's server loop) that patch is impossible: ``nest_asyncio``
refuses to patch a non-stdlib loop and raises
``ValueError: Can't patch loop of type <class 'uvloop.Loop'>``. Because the call
sat at import time, importing ``graph_rag.core`` blew up inside the request
handler, taking the whole knowledge-graph endpoint down with it (every call
answered ``{"error": "Can't patch loop of type ..."}``).

The server request path never touches those sync shims — ``get_lightrag()`` and
``get_knowledge_graph()`` are coroutines awaited by the handler — so skipping
the patch on uvloop loses nothing there. Skill/CLI entry points run on a stdlib
loop and are still patched exactly as before.

@module graph_rag.loop_patch
"""

from __future__ import annotations

import nest_asyncio
from loguru import logger

PATCHED = "patched"
"""Returned when the ambient loop is now re-entrant."""


def enable_nested_event_loops() -> str:
    """
    Best-effort re-entrant patch of the ambient event loop.

    Never raises: a loop that cannot be patched (uvloop) must not break the
    importing module, since the async paths used by the server do not need it.

    @returns ``PATCHED`` on success, otherwise a ``"skipped: <reason>"`` marker
        (also logged at debug level) describing why no patch happened.
    """
    try:
        nest_asyncio.apply()
    except ValueError as exc:
        # Raised for loops nest_asyncio cannot patch, e.g. uvloop.Loop.
        logger.debug(f"nest_asyncio patch skipped: {exc}")
        return f"skipped: {exc}"
    return PATCHED
