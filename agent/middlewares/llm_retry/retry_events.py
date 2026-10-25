"""Publish model-retry progress to the session transcript and live socket.

The LLM retry middleware runs inside ``agent/**``; it must not import the
server. The trajectory recorder is resolved through ``runtime.hooks`` and the
websocket fan-out through ``runtime.session.ws_push`` — both fail open, because
a telemetry/socket failure must never break the turn.

Wire shape (the client emits ``ws:model_retry``):

    {"event": "model_retry", "session_id": ...,
     "content": {"status": "scheduled" | "succeeded" | "exhausted",
                 "attempt": 2, "max_attempts": 11, "retries": 1,
                 "reason": "rate_limit", "delay_ms": 2000}}
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from runtime import hooks

__all__ = ["MODEL_RETRY_EVENT", "publish_model_retry"]

#: Frame event name shared with the client.
MODEL_RETRY_EVENT = "model_retry"


def publish_model_retry(
    session_id: str,
    *,
    status: str,
    attempt: int,
    max_attempts: int,
    retries: int,
    reason: str = "",
    delay_ms: float = 0.0,
) -> None:
    """Record + push one retry progress frame (best effort, never raises)."""
    session_id = (session_id or "").strip()
    if not session_id:
        return
    content: dict[str, Any] = {
        "status": status,
        "attempt": max(1, int(attempt)),
        "max_attempts": max(1, int(max_attempts)),
        "retries": max(0, int(retries)),
        "reason": str(reason or ""),
        "delay_ms": max(0, round(float(delay_ms or 0.0))),
    }
    try:
        recorder = hooks.resolve(hooks.RECORD_TRAJECTORY_EVENT)
        if recorder is not None:
            recorder(session_id, MODEL_RETRY_EVENT, content)
    except Exception:  # noqa: BLE001 - telemetry is never critical
        logger.debug("model_retry: trajectory recording failed", exc_info=True)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        loop.create_task(_push(session_id, content))
    except Exception:  # noqa: BLE001 - a dead loop must not break the retry
        logger.debug("model_retry: cannot schedule websocket push", exc_info=True)


async def _push(session_id: str, content: dict[str, Any]) -> None:
    try:
        from runtime.session.ws_push import broadcast_to_session

        sent = await broadcast_to_session(
            session_id,
            {"event": MODEL_RETRY_EVENT, "session_id": session_id, "content": content},
            ensure_ascii=False,
        )
        logger.debug(
            "model_retry: pushed {} to {} socket(s) for {}", content["status"], sent, session_id
        )
    except Exception as e:  # noqa: BLE001 - a missing viewer is the normal case
        logger.debug("model_retry: websocket push skipped for {}: {}", session_id, e)
