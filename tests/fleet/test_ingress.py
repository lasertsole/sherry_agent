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
    def __init__(self, tenant_id: str = "alice") -> None:
        self.tenant_id = tenant_id
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
        return web.json_response({"ok": True, "tenant": self.tenant_id})

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


@pytest.mark.asyncio
async def test_two_users_share_one_cell_with_independent_credentials(tmp_path: Path):
    cell = _FakeCell("team-a")
    cell_server = TestServer(cell.app)
    await cell_server.start_server()
    store = FleetStore(tmp_path / "fleet.db")
    await store.init()
    await store.create_user("alice", "password123", tenant_id="team-a")
    await store.create_user("bob", "password456", tenant_id="team-a")
    await store.upsert_cell(
        "team-a", host="127.0.0.1", port=cell_server.port, gateway_token="cell-token"
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
        alice = await client.post(
            "/auth/login", json={"username": "alice", "password": "password123"}
        )
        assert alice.status == 200
        assert (await alice.json())["user"]["tenant_id"] == "team-a"
        assert (await (await client.get("/sessions")).json())["tenant"] == "team-a"

        await client.post("/auth/logout")
        wrong = await client.post(
            "/auth/login", json={"username": "alice", "password": "password456"}
        )
        assert wrong.status == 401
        bob = await client.post("/auth/login", json={"username": "bob", "password": "password456"})
        assert bob.status == 200
        assert (await bob.json())["user"]["tenant_id"] == "team-a"
        assert (await (await client.get("/sessions")).json())["tenant"] == "team-a"
    finally:
        await client.close()
        await cell_server.close()


@pytest.mark.asyncio
async def test_a_user_switches_between_authorized_cells(tmp_path: Path):
    cell_a = _FakeCell("team-a")
    cell_b = _FakeCell("team-b")
    server_a = TestServer(cell_a.app)
    server_b = TestServer(cell_b.app)
    await server_a.start_server()
    await server_b.start_server()
    store = FleetStore(tmp_path / "fleet.db")
    await store.init()
    alice = await store.create_user("alice", "password123", tenant_id="team-a")
    await store.add_user_tenant(alice["id"], "team-b", role="member")
    await store.upsert_cell("team-a", host="127.0.0.1", port=server_a.port, gateway_token="token-a")
    await store.upsert_cell("team-b", host="127.0.0.1", port=server_b.port, gateway_token="token-b")
    await store.set_process("team-a", pid=1, status="running", data_dir="a")
    await store.set_process("team-b", pid=2, status="running", data_dir="b")
    config = IngressConfig(
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "fleet.db",
        jwt_secret="test-secret-0123456789-0123456789-abc",
        static_dir=None,
        supervise=False,
    )
    client = TestClient(TestServer(await create_app(config, store)))
    await client.start_server()
    try:
        await client.post("/auth/login", json={"username": "alice", "password": "password123"})
        listed = await (await client.get("/auth/tenants")).json()
        assert listed["active"] == "team-a"
        assert [
            (row["tenant_id"], row["role"], row["is_default"]) for row in listed["tenants"]
        ] == [
            ("team-a", "owner", True),
            ("team-b", "member", False),
        ]
        assert all(row["cell_status"] == "running" for row in listed["tenants"])
        assert (await (await client.get("/sessions")).json())["tenant"] == "team-a"

        switched = await client.post("/auth/tenant", json={"tenant_id": "team-b"})
        assert switched.status == 200
        assert (await (await client.get("/sessions")).json())["tenant"] == "team-b"
        listed = await (await client.get("/auth/tenants")).json()
        assert listed["active"] == "team-b"

        unknown = await client.post("/auth/tenant", json={"tenant_id": "team-c"})
        assert unknown.status == 403
        rejected = await client.post(
            "/auth/tenant",
            json={"tenant_id": "team-b"},
            headers={"Origin": "https://evil.test"},
        )
        assert rejected.status == 403
        assert (await rejected.json())["error"] == "csrf_rejected"

        await store.remove_user_tenant(alice["id"], "team-b")
        revoked = await client.post("/auth/tenant", json={"tenant_id": "team-b"})
        assert revoked.status == 403
        fallback = await client.get("/sessions")
        assert fallback.status == 200
        assert (await fallback.json())["tenant"] == "team-a"

        await store.add_user_tenant(alice["id"], "team-c", role="member")
        await client.post("/auth/tenant", json={"tenant_id": "team-c"})
        assert (await client.get("/sessions")).status == 503
        listed = await (await client.get("/auth/tenants")).json()
        team_c = next(row for row in listed["tenants"] if row["tenant_id"] == "team-c")
        assert team_c["cell_status"] == "stopped"

        await client.post("/auth/logout")
        await client.post("/auth/login", json={"username": "alice", "password": "password123"})
        assert (await (await client.get("/sessions")).json())["tenant"] == "team-a"
    finally:
        await client.close()
        await server_a.close()
        await server_b.close()


@pytest.mark.asyncio
async def test_a_legacy_scalar_tenant_token_still_routes(tmp_path: Path):
    import time
    import uuid

    import jwt

    from fleet.security import access_cookie

    cell = _FakeCell("team-a")
    cell_server = TestServer(cell.app)
    await cell_server.start_server()
    store = FleetStore(tmp_path / "fleet.db")
    await store.init()
    await store.create_user("alice", "password123", tenant_id="team-a")
    await store.upsert_cell(
        "team-a", host="127.0.0.1", port=cell_server.port, gateway_token="cell-token"
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
        now = int(time.time())
        legacy = jwt.encode(
            {
                "sub": "1",
                "username": "alice",
                "tenant_id": "team-a",
                "role": "user",
                "typ": "access",
                "iat": now,
                "exp": now + 900,
                "jti": uuid.uuid4().hex,
                "iss": config.issuer,
            },
            config.jwt_secret,
            algorithm="HS256",
        )
        client.session.cookie_jar.update_cookies({access_cookie(config): legacy})
        proxied = await client.get("/sessions")
        assert proxied.status == 200
        assert (await proxied.json())["tenant"] == "team-a"
    finally:
        await client.close()
        await cell_server.close()


@pytest.mark.asyncio
async def test_login_without_a_membership_is_refused(tmp_path: Path):
    store = FleetStore(tmp_path / "fleet.db")
    await store.init()
    alice = await store.create_user("alice", "password123", tenant_id="team-a")
    assert await store.remove_user_tenant(alice["id"], "team-a") is True
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
        response = await client.post(
            "/auth/login", json={"username": "alice", "password": "password123"}
        )
        assert response.status == 403
        assert (await response.json())["error"] == "no_tenant"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_an_old_refresh_token_upgrades_to_the_membership_claims(tmp_path: Path):
    import time
    import uuid

    import jwt

    from fleet.security import refresh_cookie

    cell = _FakeCell("team-a")
    cell_server = TestServer(cell.app)
    await cell_server.start_server()
    store = FleetStore(tmp_path / "fleet.db")
    await store.init()
    alice = await store.create_user("alice", "password123", tenant_id="team-a")
    await store.upsert_cell(
        "team-a", host="127.0.0.1", port=cell_server.port, gateway_token="cell-token"
    )
    config = IngressConfig(
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "fleet.db",
        jwt_secret="test-secret-0123456789-0123456789-abc",
        static_dir=None,
    )
    now = int(time.time())
    jti = uuid.uuid4().hex
    await store.save_refresh(jti, alice["id"], now + 3600)
    old_refresh = jwt.encode(
        {
            "sub": str(alice["id"]),
            "username": "alice",
            "tenant_id": "team-a",
            "role": "user",
            "typ": "refresh",
            "iat": now,
            "exp": now + 3600,
            "jti": jti,
            "iss": config.issuer,
        },
        config.jwt_secret,
        algorithm="HS256",
    )
    client = TestClient(TestServer(await create_app(config, store)))
    await client.start_server()
    try:
        client.session.cookie_jar.update_cookies({refresh_cookie(config): old_refresh})
        refreshed = await client.post("/auth/refresh")
        assert refreshed.status == 200
        assert (await refreshed.json())["user"]["tenant_id"] == "team-a"
        listed = await (await client.get("/auth/tenants")).json()
        assert listed["active"] == "team-a"
        assert [row["tenant_id"] for row in listed["tenants"]] == ["team-a"]
        assert (await client.get("/sessions")).status == 200
    finally:
        await client.close()
        await cell_server.close()


@pytest.mark.asyncio
async def test_accounts_on_different_cells_are_routed_apart(tmp_path: Path):
    cell_a = _FakeCell("team-a")
    cell_b = _FakeCell("team-b")
    server_a = TestServer(cell_a.app)
    server_b = TestServer(cell_b.app)
    await server_a.start_server()
    await server_b.start_server()
    store = FleetStore(tmp_path / "fleet.db")
    await store.init()
    await store.create_user("alice", "password123", tenant_id="team-a")
    await store.create_user("carol", "password789", tenant_id="team-b")
    await store.upsert_cell("team-a", host="127.0.0.1", port=server_a.port, gateway_token="token-a")
    await store.upsert_cell("team-b", host="127.0.0.1", port=server_b.port, gateway_token="token-b")
    config = IngressConfig(
        host="127.0.0.1",
        port=0,
        db_path=tmp_path / "fleet.db",
        jwt_secret="test-secret-0123456789-0123456789-abc",
        static_dir=None,
        supervise=False,
    )
    client = TestClient(TestServer(await create_app(config, store)))
    await client.start_server()
    try:
        await client.post("/auth/login", json={"username": "alice", "password": "password123"})
        assert (await (await client.get("/sessions")).json())["tenant"] == "team-a"

        await client.post("/auth/logout")
        await client.post("/auth/login", json={"username": "carol", "password": "password789"})
        assert (await (await client.get("/sessions")).json())["tenant"] == "team-b"
    finally:
        await client.close()
        await server_a.close()
        await server_b.close()
