"""Loopback seam for the super-agent side process.

The P-Spike route (``/internal/test``) proved the two directions work.  This
module adds the real ``/internal/superagent/*`` surface.  Sherry core
(agent/, state_port, spawn core, turn runner) is untouched: each endpoint
dispatches to a registered synchronous or asynchronous hook, so the side
process can be tested with a fake backend and Sherry stays import-safe.

* side process → Sherry: gateway-authenticated HTTP calls below.
* Sherry → side process: ``emit_event`` posts a trajectory/completion event to
  the superagent process; ``set_outbound_hook`` keeps the in-process probe used
  by the spike tests.
"""

from __future__ import annotations

from collections.abc import Callable
from inspect import isawaitable
from typing import Any

from robyn import Request, Response

from server.trigger.core import app
from server.trigger.http.helpers import bad_request, ok, read_body

OutboundHook = Callable[[dict[str, Any]], dict[str, Any]]
Hook = Callable[[dict[str, Any]], Any]

_outbound: OutboundHook | None = None
_hooks: dict[str, Hook] = {}
_events_url: str = ""


def set_outbound_hook(hook: OutboundHook | None) -> None:
    """Register (or clear) the Sherry→side-process probe callback."""
    global _outbound
    _outbound = hook


def register_hook(name: str, hook: Hook | None) -> None:
    """Register (or clear) one loopback handler."""
    if hook is None:
        _hooks.pop(name, None)
    else:
        _hooks[name] = hook


def clear_hooks() -> None:
    """Clear all registered loopback handlers (tests and shutdown)."""
    _hooks.clear()


def set_events_url(url: str) -> None:
    """Set the superagent event callback URL."""
    global _events_url
    _events_url = url.rstrip("/")


async def emit_event(event: dict[str, Any]) -> bool:
    """Send one Sherry→superagent event; return False when unconfigured."""
    if not _events_url:
        return False
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.post(f"{_events_url}/internal/events", json=event) as response:
            return response.status < 400


async def dispatch(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke a registered hook and normalise its result."""
    hook = _hooks.get(name)
    if hook is None:
        return {"ok": True, "handled": False}
    result = hook(payload)
    if isawaitable(result):
        result = await result
    if not isinstance(result, dict):
        result = {"value": result}
    return {"ok": True, "handled": True, **result}


@app.post("/internal/test")
async def internal_test_handler(request: Request) -> Response:
    """Echo a loopback probe and invoke the reverse-direction hook, if set."""
    body = read_body(request)
    if body is None:
        return bad_request("JSON object body is required")

    outbound: dict[str, Any] | None = None
    if _outbound is not None:
        try:
            outbound = _outbound({"kind": "test", "payload": body})
        except Exception as exc:  # noqa: BLE001 — probe must not 500 the caller
            outbound = {"ok": False, "error": type(exc).__name__}

    return ok({"ok": True, "echo": body.get("echo"), "outbound": outbound})


@app.post("/internal/superagent/spawn")
async def spawn_handler(request: Request) -> Response:
    """Spawn a worker subagent of the persistent superagent main Session."""
    body = read_body(request)
    if body is None:
        return bad_request("JSON object body is required")
    return ok(await dispatch("spawn", body))


@app.post("/internal/superagent/cancel")
async def cancel_handler(request: Request) -> Response:
    """Cancel a worker run."""
    body = read_body(request)
    if body is None:
        return bad_request("JSON object body is required")
    return ok(await dispatch("cancel", body))


@app.post("/internal/superagent/session/ensure")
async def session_ensure_handler(request: Request) -> Response:
    """Create or reuse the persistent superagent main Session."""
    body = read_body(request) or {}
    return ok(await dispatch("session.ensure", body))


@app.post("/internal/superagent/session/exists")
async def session_exists_handler(request: Request) -> Response:
    """Report whether the superagent main Session still exists."""
    body = read_body(request) or {}
    return ok(await dispatch("session.exists", {"key": body.get("key", "superagent-main")}))


@app.post("/internal/superagent/session/inject")
async def session_inject_handler(request: Request) -> Response:
    """Inject a candidate or notice into the main Session."""
    body = read_body(request)
    if body is None:
        return bad_request("JSON object body is required")
    return ok(await dispatch("session.inject", body))


@app.post("/internal/superagent/session/heal")
async def session_heal_handler(request: Request) -> Response:
    """Heal an interrupted turn after a restart."""
    body = read_body(request)
    if body is None:
        return bad_request("JSON object body is required")
    return ok(await dispatch("session.heal", body))


@app.post("/internal/superagent/session/add_messages")
async def session_add_messages_handler(request: Request) -> Response:
    """Deliver queued Inputs into a running worker Session."""
    body = read_body(request)
    if body is None:
        return bad_request("JSON object body is required")
    return ok(await dispatch("session.add_messages", body))


@app.post("/internal/superagent/session/truncate")
async def session_truncate_handler(request: Request) -> Response:
    """Truncate messages after a turn during a retry."""
    body = read_body(request)
    if body is None:
        return bad_request("JSON object body is required")
    return ok(await dispatch("session.truncate", body))


@app.post("/internal/superagent/session/state")
async def session_state_handler(request: Request) -> Response:
    """Read a Session state snapshot."""
    body = read_body(request) or {}
    return ok(await dispatch("session.state", {"key": body.get("key", "")}))


@app.post("/internal/superagent/session/jobs")
async def session_jobs_handler(request: Request) -> Response:
    """List a Session's background jobs."""
    body = read_body(request) or {}
    return ok(await dispatch("session.jobs", {"key": body.get("key", "")}))


@app.post("/internal/superagent/session/kill_job")
async def session_kill_job_handler(request: Request) -> Response:
    """Kill one background job."""
    body = read_body(request)
    if body is None:
        return bad_request("JSON object body is required")
    return ok(await dispatch("session.kill_job", body))


@app.post("/internal/superagent/session/trajectory")
async def session_trajectory_handler(request: Request) -> Response:
    """Read a Session's trajectory events."""
    body = read_body(request) or {}
    return ok(await dispatch("session.trajectory", {"key": body.get("key", "")}))


__all__ = [
    "clear_hooks",
    "dispatch",
    "emit_event",
    "register_hook",
    "set_events_url",
    "set_outbound_hook",
]
