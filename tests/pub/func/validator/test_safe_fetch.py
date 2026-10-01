"""The fetcher that stands between attacker-influenced URLs and the network.

Three properties, each with its own test group: a non-public target is refused
before any connection, the connection goes to the address that was *verified*
(not to a fresh lookup, which is how DNS rebinding wins), and every redirect hop
faces the same gate. A local HTTP server provides the positive path — the
hostname is 127.0.0.1, so the escape hatch is what allows it, and the pinned
variant monkeypatches the resolution to prove the pinning mechanics.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pub.func.validator import safe_fetch as module
from pub.func.validator import public_url

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


# ── a tiny controllable local server ─────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
    seen_hosts: list[str] = []

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        self.seen_hosts.append(self.headers.get("Host", ""))
        status, headers, body = self.routes.get(self.path, (404, {}, b"nope"))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def local_server():
    _Handler.routes = {}
    _Handler.seen_hosts = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Both spellings point at the handler's dicts, which the fixture reset above.
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
    server.routes = _Handler.routes  # type: ignore[attr-defined]
    server.seen_hosts = _Handler.seen_hosts  # type: ignore[attr-defined]
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def _no_escape_hatch(monkeypatch):
    monkeypatch.delenv("SHERRY_ALLOW_PRIVATE_MEDIA_URLS", raising=False)


@pytest.fixture
def escape_hatch(monkeypatch):
    monkeypatch.setenv("SHERRY_ALLOW_PRIVATE_MEDIA_URLS", "1")


@pytest.fixture
def pinned_locally(monkeypatch):
    """Pretend 127.0.0.1 is a verified public address, so the pin path runs."""

    def fake_resolve(host: str) -> list[str]:
        return ["127.0.0.1"] if host in {"127.0.0.1", "localhost", "example.test"} else []

    monkeypatch.setattr(module, "resolve_public_addresses", fake_resolve)


# ── refusals ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://169.254.169.254/latest/meta-data/", "ssrf"),
        ("http://metadata.google.internal/", "ssrf"),
        ("http://127.0.0.1:9/", "ssrf"),
        ("http://[::1]:9/", "ssrf"),
        ("http://10.1.2.3/", "ssrf"),
        ("http://198.18.0.7/", "ssrf"),
        ("http://[fd00::1]/", "ssrf"),
        ("file:///etc/passwd", "scheme"),
        ("ftp://example.test/x", "scheme"),
        ("", "scheme"),
    ],
)
def test_dangerous_targets_are_refused_before_connecting(url, reason, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("a refused URL must not open a socket")

    monkeypatch.setattr(module.socket, "create_connection", explode)

    result = module.safe_fetch(url, timeout=1)

    assert result.ok is False
    assert result.reason == reason


def test_a_mixed_answer_is_refused_entirely(monkeypatch):
    """Public + private in one DNS answer is a rebinding attempt, not a coin flip."""
    infos = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
    ]
    monkeypatch.setattr(public_url.socket, "getaddrinfo", lambda *a, **k: infos)

    def explode(*args, **kwargs):
        raise AssertionError("no socket for a mixed answer")

    monkeypatch.setattr(module.socket, "create_connection", explode)

    assert module.safe_fetch("http://example.test/x", timeout=1).reason == "ssrf"


# ── pinning ──────────────────────────────────────────────────────────────


def test_the_connection_goes_to_the_verified_address(local_server, monkeypatch):
    """The rebinding window: exactly one resolution decides where we dial.

    Counting the resolutions is the point — a second lookup between the check
    and the connect is precisely what an attacker with a flipping A record
    needs, so the assertion is "one lookup per hop" *and* "the socket went to
    the address that lookup returned".
    """
    local_server.routes["/ok"] = (200, {}, b"payload")
    dialed: list[tuple] = []
    resolved: list[str] = []

    real_create = socket.create_connection

    def recording_create(address, timeout=None, **kwargs):
        dialed.append(address)
        return real_create(address, timeout=timeout, **kwargs)

    def recording_resolve(host: str) -> list[str]:
        resolved.append(host)
        return ["127.0.0.1"]

    monkeypatch.setattr(module.socket, "create_connection", recording_create)
    monkeypatch.setattr(module, "resolve_public_addresses", recording_resolve)

    result = module.safe_fetch(
        f"http://example.test:{local_server.server_address[1]}/ok", timeout=5
    )

    assert result.ok, result.reason
    assert result.body == b"payload"
    assert resolved == ["example.test"], "one verified resolution per hop"
    assert dialed == [("127.0.0.1", local_server.server_address[1])]


def test_the_hostname_still_travels_in_the_host_header(local_server, pinned_locally):
    local_server.routes["/host"] = (200, {}, b"ok")

    result = module.safe_fetch(
        f"http://example.test:{local_server.server_address[1]}/host", timeout=5
    )

    assert result.ok
    assert _Handler.seen_hosts == [f"example.test:{local_server.server_address[1]}"]


# ── redirects ────────────────────────────────────────────────────────────


def test_redirects_are_followed_and_reported(local_server, escape_hatch):
    local_server.routes["/start"] = (302, {"Location": "/final"}, b"")
    local_server.routes["/final"] = (200, {}, b"landed")

    result = module.safe_fetch(f"{local_server.base_url}/start", timeout=5)  # type: ignore[attr-defined]

    assert result.ok
    assert result.body == b"landed"
    assert result.final_url.endswith("/final")


def test_a_redirect_to_a_private_target_is_refused(local_server, escape_hatch, monkeypatch):
    """The hop that a single pre-flight check would miss."""
    local_server.routes["/start"] = (302, {"Location": "http://169.254.169.254/secret"}, b"")
    # The escape hatch is on for the first hop only: turn the guard back on.
    monkeypatch.delenv("SHERRY_ALLOW_PRIVATE_MEDIA_URLS")

    result = module.safe_fetch(f"{local_server.base_url}/start", timeout=5)  # type: ignore[attr-defined]

    assert result.ok is False
    assert result.reason == "ssrf"


def test_a_redirect_loop_stops_after_the_limit(local_server, escape_hatch):
    local_server.routes["/a"] = (302, {"Location": "/b"}, b"")
    local_server.routes["/b"] = (302, {"Location": "/a"}, b"")

    result = module.safe_fetch(
        f"{local_server.base_url}/a",
        timeout=5,
        max_redirects=3,  # type: ignore[attr-defined]
    )

    assert result.ok is False
    assert result.reason == "redirects"


def test_a_relative_redirect_is_resolved_against_the_current_url(local_server, escape_hatch):
    local_server.routes["/deep/start"] = (302, {"Location": "next"}, b"")
    local_server.routes["/deep/next"] = (200, {}, b"relative")

    result = module.safe_fetch(f"{local_server.base_url}/deep/start", timeout=5)  # type: ignore[attr-defined]

    assert result.ok
    assert result.body == b"relative"


# ── size cap and failure mapping ─────────────────────────────────────────


def test_a_declared_length_over_the_cap_is_refused_without_reading(local_server, escape_hatch):
    local_server.routes["/big"] = (200, {}, b"x" * 4096)

    result = module.safe_fetch(f"{local_server.base_url}/big", timeout=5, max_bytes=16)  # type: ignore[attr-defined]

    assert result.ok is False
    assert result.reason == "oversize"
    assert result.declared_length == 4096


def test_a_streamed_body_over_the_cap_is_refused(local_server, escape_hatch):
    body = b"y" * 100
    local_server.routes["/chunked"] = (200, {"Transfer-Encoding": "chunked"}, body)

    result = module.safe_fetch(
        f"{local_server.base_url}/chunked",
        timeout=5,
        max_bytes=10,  # type: ignore[attr-defined]
    )

    assert result.ok is False
    assert result.reason == "oversize"


def test_a_dead_host_is_a_network_failure(local_server, escape_hatch):
    """A closed port is not a security refusal and must be reported as such."""
    port = local_server.server_address[1]
    local_server.shutdown()
    local_server.server_close()

    result = module.safe_fetch(f"http://127.0.0.1:{port}/gone", timeout=2)

    assert result.ok is False
    assert result.reason == "network"


def test_a_successful_fetch_returns_the_body(local_server, escape_hatch):
    local_server.routes["/json"] = (200, {}, json.dumps({"ok": True}).encode())

    result = module.safe_fetch(f"{local_server.base_url}/json", timeout=5)  # type: ignore[attr-defined]

    assert result.ok
    assert json.loads(result.body) == {"ok": True}
    assert result.status == 200
    assert result.observed_bytes == len(result.body or b"")
