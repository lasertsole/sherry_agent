"""Process-level liveness endpoint (``GET /health``).

Design concept #22 (health checks): before this route the client could only
INFER the backend was alive from websocket evidence — which is exactly backwards
when the question is "is the backend up at all" (the connectivity banner's
fallback path). This is a SELF-check only: process uptime, the model
configuration state, the live-socket session count and the queue depth. It
probes no downstream service, so it is O(1) and every collector fails soft to
``null`` — a health endpoint that can 5xx is not a health endpoint.
"""

from __future__ import annotations

import os
import time
from importlib.metadata import PackageNotFoundError, version

from loguru import logger
from robyn import Request, Response

from server.trigger.core import app
from server.trigger.http.helpers import ok

_STARTED_MONOTONIC = time.monotonic()

#: The packaged backend tree ships a VERSION marker; a checkout reports the
#: installed distribution (or "unknown" — the field is informational).
_PACKAGE_NAME = "ema-ai-agent"


def _app_version() -> str:
    try:
        return version(_PACKAGE_NAME)
    except PackageNotFoundError:
        home = os.getenv("SHERRY_HOME", "")
        try:
            with open(os.path.join(home, "sherry-backend", "VERSION"), encoding="utf-8") as fh:
                return fh.read().strip() or "unknown"
        except OSError:
            return "unknown"


def _models_configured() -> bool:
    """The same shape the boot gate means by "configured": a main model identity."""
    return bool(os.getenv("MAIN_LLM_PROVIDER", "").strip()) and bool(
        os.getenv("MAIN_LLM_NAME", "").strip()
    )


def _live_session_count() -> int:
    """Sessions with at least one live websocket (a count, never the ids)."""
    from runtime.session.relation_register import relation_register

    sessions = {
        relation_register.get_session_id_by_websocket(ws)
        for ws in relation_register.get_all_websockets()
    }
    sessions.discard(None)
    return len(sessions)


async def health_payload() -> dict[str, object]:
    """The self-check body. Every collector is individually optional."""
    payload: dict[str, object] = {
        "ok": True,
        "version": _app_version(),
        "uptime_s": int(time.monotonic() - _STARTED_MONOTONIC),
        "models_configured": _models_configured(),
        "live_sessions": None,
        "queue_depth": None,
    }
    try:
        payload["live_sessions"] = _live_session_count()
    except Exception as exc:  # noqa: BLE001 — a gauge must not fail the check
        logger.warning("health: live_sessions collector failed: {}", exc)
    try:
        from server.service.input_queue_service import get_default_queue

        payload["queue_depth"] = await get_default_queue().total_queued()
    except Exception as exc:  # noqa: BLE001 — a gauge must not fail the check
        logger.warning("health: queue_depth collector failed: {}", exc)
    return payload


@app.get("/health")
async def health_handler(request: Request) -> Response:  # noqa: ARG001 - Robyn's signature
    """Process self-check: ``{ok, version, uptime_s, models_configured, live_sessions, queue_depth}``."""
    return ok(await health_payload())
