"""Ingress auth + HTTP/WS proxy against a fake cell."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from fleet.config import IngressConfig
from fleet.ingress import create_app
from fleet.store import FleetStore

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


class _FakeCell:
    def __init__(self) -> None:
        self.seen: dict[str, Any] = {}
        self.app = web.Application()
        self.app.router.add_get("/sessions", self.sessions)
        self.app.router.add_get("/auth/token", self.auth_token)
        self.app.router.add_get("/auth/ws-ticket", self.ws_ticket)
        self.app.router.add_get("/sessions/agent/ws", self.agent_ws)

    async def sessions(self, request: web.Request) -> web.Response:
        self.seen = {
            "token": request.headers.get("token"),
            "origin": request.headers.get("Origin"),
            "cookie": request.headers.get("Cookie"),
        }
        return web.json_response({"ok": True, "tenant": "alice"})

    async def auth_token(self, _request: web.Request) -> web.Response:
        return web.json_response({"token": "CELL-GATEWAY-SECRET"})

    async def ws_ticket(self, _request: web.Request) -> web.Response:
        return web.json_response({"ticket": "ticket-1"})

    async def agent_ws(self, request: web.Request) -> web.WebSocketResponse:
        self.seen["ws_ticket"] = request.rel_url.query.get("ticket")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                await ws.send_str(f"echo:{message.data}")
        return ws


@pytest.mark.asyncio
async def test_login_proxy_and_auth_token_is_never_forwarded(tmp_path: Path):
    cell = _FakeCell()
    cell_server = TestServer(cell.app)
    await cell_server.start_server()
    store = FleetStore(tmp_path / "fleet.db")
    await store.init()
    await store.create_user("alice", "password123", tenant_id="alice")
    await store.upsert_cell(
        "alice",
        host="127.0.0.1",
        port=cell_server.port,
        gateway_token="cell-gateway-token",
    )
    config = IngressConfig(
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "fleet.db",
        jwt_secret="test-secret-0123456789-0123456789-abc",
        static_dir=None,
    )
    app = await create_app(config, store)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        assert (await client.get("/sessions")).status == 401

        login = await client.post(
            "/auth/login", json={"username": "alice", "password": "password123"}
        )
        assert login.status == 200
        assert (await login.json())["user"]["tenant_id"] == "alice"

        proxied = await client.get("/sessions", headers={"token": "stale-browser-token"})
        assert proxied.status == 200
        assert await proxied.json() == {"ok": True, "tenant": "alice"}
        assert cell.seen == {
            "token": "cell-gateway-token",
            "origin": None,
            "cookie": None,
        }

        blocked = await client.get("/auth/token")
        assert blocked.status == 404
        assert "CELL-GATEWAY-SECRET" not in await blocked.text()

        rejected = await client.post("/sessions", headers={"Origin": "https://evil.test"})
        assert rejected.status == 403
    finally:
        await client.close()
        await cell_server.close()


@pytest.mark.asyncio
async def test_websocket_is_proxied_with_a_cell_ticket(tmp_path: Path):
    cell = _FakeCell()
    cell_server = TestServer(cell.app)
    await cell_server.start_server()
    store = FleetStore(tmp_path / "fleet.db")
    await store.init()
    await store.create_user("alice", "password123", tenant_id="alice")
    await store.upsert_cell(
        "alice",
        host="127.0.0.1",
        port=cell_server.port,
        gateway_token="cell-gateway-token",
    )
    config = IngressConfig(
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "fleet.db",
        jwt_secret="test-secret-0123456789-0123456789-abc",
        static_dir=None,
    )
    client = TestClient(TestServer(await create_app(config, store)))
    await client.start_server()
    try:
        await client.post("/auth/login", json={"username": "alice", "password": "password123"})
        ws = await client.ws_connect("/sessions/agent/ws?session_id=s&ticket=evil")
        await ws.send_str("hello")
        message = await ws.receive(timeout=5)
        assert message.type == WSMsgType.TEXT
        assert message.data == "echo:hello"
        assert cell.seen["ws_ticket"] == "ticket-1"
        await ws.close()

        logout = await client.post("/auth/logout")
        assert logout.status == 200
        assert (await client.get("/sessions")).status == 401
    finally:
        await client.close()
        await cell_server.close()


@pytest.mark.asyncio
async def test_failed_logins_are_rate_limited(tmp_path: Path):
    store = FleetStore(tmp_path / "fleet.db")
    await store.init()
    await store.create_user("alice", "password123", tenant_id="alice")
    config = IngressConfig(
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "fleet.db",
        jwt_secret="test-secret-0123456789-0123456789-abc",
        static_dir=None,
    )
    client = TestClient(TestServer(await create_app(config, store)))
    await client.start_server()
    try:
        for _ in range(5):
            response = await client.post(
                "/auth/login", json={"username": "alice", "password": "wrong"}
            )
            assert response.status == 401
        blocked = await client.post(
            "/auth/login", json={"username": "alice", "password": "password123"}
        )
        assert blocked.status == 429
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_the_static_spa_root_and_assets_are_public(tmp_path: Path):
    static_dir = tmp_path / "public"
    (static_dir / "_nuxt").mkdir(parents=True)
    (static_dir / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (static_dir / "200.html").write_text("<html>deep-route</html>", encoding="utf-8")
    (static_dir / "_nuxt" / "app.js").write_text("console.log(1)", encoding="utf-8")
    store = FleetStore(tmp_path / "fleet.db")
    await store.init()
    await store.create_user("alice", "password123", tenant_id="alice")
    config = IngressConfig(
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "fleet.db",
        jwt_secret="test-secret-0123456789-0123456789-abc",
        static_dir=static_dir,
    )
    client = TestClient(TestServer(await create_app(config, store)))
    await client.start_server()
    try:
        root = await client.get("/")
        assert root.status == 200
        assert await root.text() == "<html>spa</html>"
        asset = await client.get("/_nuxt/app.js")
        assert asset.status == 200
        assert await asset.text() == "console.log(1)"

        login_shell = await client.get("/login", headers={"Accept": "text/html"})
        assert login_shell.status == 200
        assert await login_shell.text() == "<html>deep-route</html>"
        assert (await client.get("/home/sess-1", headers={"Accept": "text/html"})).status == 200
        assert (
            await client.get("/home/sess-1", headers={"Accept": "application/json"})
        ).status == 401
        await client.post("/auth/login", json={"username": "alice", "password": "password123"})
        deep = await client.get("/home/sess-1", headers={"Accept": "text/html"})
        assert deep.status == 200
        assert await deep.text() == "<html>deep-route</html>"
        api = await client.get("/home/sess-1", headers={"Accept": "application/json"})
        assert api.status == 503
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_the_auth_surface_matches_the_shared_spa_contract(tmp_path: Path):
    store = FleetStore(tmp_path / "fleet.db")
    await store.init()
    await store.create_user("alice", "password123", tenant_id="alice")
    config = IngressConfig(
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "fleet.db",
        jwt_secret="test-secret-0123456789-0123456789-abc",
        static_dir=None,
    )
    client = TestClient(TestServer(await create_app(config, store)))
    await client.start_server()
    try:
        status = await (await client.get("/auth/status")).json()
        assert status["authenticated"] is False
        assert status["auth_required"] is True
        assert status["auth_enabled"] is True
        assert status["has_account"] is True
        assert status["username_required"] is True

        login = await client.post(
            "/auth/login", json={"username": "alice", "password": "password123"}
        )
        body = await login.json()
        assert body["user"]["username"] == "alice"
        assert body["expires_in"] > 0

        me = await (await client.get("/auth/me")).json()
        assert me["user"]["tenant_id"] == "alice"

        refreshed = await (await client.post("/auth/refresh")).json()
        assert refreshed["user"]["username"] == "alice"
        assert refreshed["expires_in"] > 0

        changed = await client.put(
            "/auth/account",
            json={"password": "password123", "new_password": "newpassword123"},
        )
        assert changed.status == 200
        assert (await client.get("/auth/me")).status == 200

        refused = await client.put(
            "/auth/account", json={"password": "newpassword123", "enabled": True}
        )
        assert refused.status == 403
        assert (await refused.json())["error"] == "managed_by_admin"
        assert (await client.delete("/auth/account")).status == 403

        await client.post("/auth/logout")
        assert (await client.get("/auth/me")).status == 401
        stale = await client.post(
            "/auth/login", json={"username": "alice", "password": "password123"}
        )
        assert stale.status == 401
        fresh = await client.post(
            "/auth/login", json={"username": "alice", "password": "newpassword123"}
        )
        assert fresh.status == 200
    finally:
        await client.close()


def test_login_limiter_uses_the_forwarded_address_only_when_trusted(tmp_path: Path):
    from types import SimpleNamespace

    from fleet.ingress import CONFIG_KEY, _login_key

    config = IngressConfig(
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "fleet.db",
        jwt_secret="x" * 40,
        trust_forwarded=True,
    )
    request = SimpleNamespace(
        remote="10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
        app={CONFIG_KEY: config},
    )

    assert _login_key(request, "Alice") == "203.0.113.7:alice"

    plain = SimpleNamespace(
        remote="10.0.0.5",
        headers={"X-Forwarded-For": "203.0.113.7"},
        app={
            CONFIG_KEY: IngressConfig(
                host="127.0.0.1",
                port=0,
                db_path=tmp_path / "fleet.db",
                jwt_secret="x" * 40,
            )
        },
    )
    assert _login_key(plain, "alice") == "10.0.0.5:alice"
