"""A minimal cell process used by the real-spawn Fleet test.

It honours the cell contract the manager and Ingress rely on: ``SHERRY_HOME``
in the environment, ``/health`` for readiness, the gateway ``token`` header on
every API route, and an ``/auth/ws-ticket`` + WebSocket pair.
"""

from __future__ import annotations

import os

from aiohttp import WSMsgType, web

_TOKEN = os.environ.get("SHERRY_GATEWAY_TOKEN", "")


def _authorized(request: web.Request) -> bool:
    return request.headers.get("token") == _TOKEN


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ready": True})


async def sessions(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.json_response({"success": False}, status=401)
    return web.json_response({"tenant": os.path.basename(str(os.environ.get("SHERRY_HOME", "")))})


async def ws_ticket(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.json_response({"success": False}, status=401)
    return web.json_response({"ticket": "e2e-ticket"})


async def agent_ws(request: web.Request) -> web.StreamResponse:
    if request.query.get("token") != _TOKEN or request.query.get("ticket") != "e2e-ticket":
        return web.json_response({"success": False}, status=401)
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    async for message in ws:
        if message.type == WSMsgType.TEXT:
            await ws.send_str(f"echo:{message.data}")
    return ws


def main() -> None:
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/sessions", sessions)
    app.router.add_get("/auth/ws-ticket", ws_ticket)
    app.router.add_get("/sessions/agent/ws", agent_ws)
    web.run_app(app, host="127.0.0.1", port=int(os.environ["API_PORT"]), print=None)


if __name__ == "__main__":
    main()
