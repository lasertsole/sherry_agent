"""Shared best-effort WebSocket frame send (audit 2.1.2).

Three modules previously carried a private ``_send_ws`` with the same
contract — ``server/trigger/ws/messages.py``, ``server/service/turn_runner.py``
and ``server/service/auto_turn.py``:

- a ``None`` socket is skipped (frames are skippable — the socket may be gone
  at any moment, delivery must never break the turn),
- the payload is serialized with :func:`json.dumps` and sent via
  ``await websocket.send_text`` (Robyn's send is a coroutine — it MUST be
  awaited or the frame is silently dropped),
- any send failure is swallowed and logged (the socket may be closing).

The per-site differences are preserved as parameters: ``ensure_ascii`` (the
auto-turn path historically serialized with ``ensure_ascii=False``) and
``warn_prefix`` (each site kept its own log wording).
"""

import json
from typing import Any

from loguru import logger


async def send_ws_json(
    websocket: Any,
    payload: dict[str, Any],
    *,
    ensure_ascii: bool = True,
    warn_prefix: str = "WS send failed",
) -> None:
    """Best-effort JSON frame send; never raises.

    Args:
        websocket: The target socket (``None`` → no-op).
        payload: JSON-serializable frame body.
        ensure_ascii: Passed through to :func:`json.dumps`.
        warn_prefix: Full loguru warning prefix used on send failure, so each
            call site keeps its original log wording
            (e.g. ``"Agent WS send failed"`` → ``"Agent WS send failed: {e}"``).
    """
    if websocket is None:
        return
    try:
        # Robyn's WebSocket.send_text is a coroutine — it MUST be awaited or
        # the frame is silently dropped (fire-and-forget coroutine leak).
        await websocket.send_text(json.dumps(payload, ensure_ascii=ensure_ascii))
    except Exception as e:  # noqa: BLE001 - delivery must never break the caller
        logger.warning(f"{warn_prefix}: {e}")
