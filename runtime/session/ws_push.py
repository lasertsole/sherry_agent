"""Best-effort WebSocket broadcast.

Every live socket receives each pushed frame **once** (there is no replay log
and no per-socket queue): :func:`broadcast_to_session` fans out over
``relation_register.get_websockets_by_session_id``; :func:`broadcast_to_all`
reaches every registered socket for process-level work such as model download
progress. A socket whose send fails is pruned so the next broadcast skips it.

Lives in ``runtime`` (not ``server.utils``) because agent-side producers
(taskflow progress, todo pushes, web-search fallback notices, background
terminal exits, subagent events) push through it too, and ``agent/**`` must not
import ``server/**``.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from .relation_register import relation_register

__all__ = ["broadcast_to_all", "broadcast_to_session"]


async def _broadcast(
    sockets: list[Any],
    payload: dict[str, Any],
    *,
    ensure_ascii: bool,
    warn_prefix: str,
    scope: str,
) -> int:
    """Send one serialized frame to each socket; a failure prunes only that socket."""
    if not sockets:
        return 0
    text = json.dumps(payload, ensure_ascii=ensure_ascii)
    sent = 0
    for websocket in sockets:
        try:
            await websocket.send_text(text)
            sent += 1
        except Exception as e:  # noqa: BLE001 — one dead socket must not stop the others
            logger.warning(
                "{}: {} websocket={}: {}",
                warn_prefix,
                scope,
                getattr(websocket, "id", "?"),
                e,
            )
            # Prune the dead socket so the next broadcast skips it. Best-effort:
            # a test double (or a partially constructed socket) may not be
            # registry-backed, and pruning must never mask the send failure.
            try:
                relation_register.unregister_websocket_by_websocket(websocket)
            except Exception:  # noqa: BLE001, S110 — pruning must never mask the send failure
                logger.debug(
                    "WS broadcast: prune failed for websocket={}", getattr(websocket, "id", "?")
                )
    return sent


async def broadcast_to_session(
    session_id: str,
    payload: dict[str, Any],
    *,
    ensure_ascii: bool = True,
    warn_prefix: str = "WS broadcast failed",
) -> int:
    """Send ``payload`` once to every live socket of ``session_id``.

    The payload is serialized ONCE and the same text is delivered to each
    socket (no re-serialization, no per-socket copies of different content).
    A missing socket set is the normal quiet case. A failed send only removes
    that socket; the remaining sockets still receive the frame.

    Returns the number of sockets the frame was delivered to.
    """
    return await _broadcast(
        relation_register.get_websockets_by_session_id(session_id),
        payload,
        ensure_ascii=ensure_ascii,
        warn_prefix=warn_prefix,
        scope=f"session={session_id}",
    )


async def broadcast_to_all(
    payload: dict[str, Any],
    *,
    ensure_ascii: bool = True,
    warn_prefix: str = "WS broadcast failed",
) -> int:
    """Send ``payload`` once to EVERY live socket, whatever its session.

    Used by process-level (non-session-scoped) work such as model download
    progress: the settings panel is not tied to the session whose socket
    happens to be open. Same best-effort contract as
    :func:`broadcast_to_session`: the frame is serialized once, a dead socket
    is pruned, and the remaining sockets still receive it.
    """
    return await _broadcast(
        relation_register.get_all_websockets(),
        payload,
        ensure_ascii=ensure_ascii,
        warn_prefix=warn_prefix,
        scope="all sessions",
    )
