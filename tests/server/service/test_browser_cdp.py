"""The CDP transport: id correlation, flat sessions, events, failures.

The transport is protocol-thin on purpose — these tests pin exactly the
contract ``browser_manager`` builds on: a response is matched to its id, a
protocol error surfaces as :class:`CdpError`, a timeout does not hang, and a
socket that dies fails the pending calls instead of leaving them awaiting.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from server.service import browser_cdp

pytestmark = [pytest.mark.unit]


class _FakeBrowser:
    """A minimal CDP endpoint: echoes results, can error / go silent / emit."""

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.conns: list = []

    async def handler(self, ws) -> None:
        self.conns.append(ws)
        async for raw in ws:
            message = json.loads(raw)
            self.received.append(message)
            method = message["method"]
            if method == "Boom":
                await ws.send(json.dumps({"id": message["id"], "error": {"message": "nope"}}))
                continue
            if method == "Silent":
                continue
            if method == "Emit":
                await ws.send(
                    json.dumps({"method": "Page.loadEventFired", "sessionId": "S1", "params": {}})
                )
            await ws.send(
                json.dumps(
                    {
                        "id": message["id"],
                        "result": {"method": method, "sessionId": message.get("sessionId")},
                    }
                )
            )


def _run(scenario) -> None:
    asyncio.run(scenario())


def test_call_correlates_ids_and_carries_the_flat_session():
    async def scenario() -> None:
        fake = _FakeBrowser()
        async with websockets.serve(fake.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            conn = browser_cdp.CdpConnection(f"ws://127.0.0.1:{port}/devtools/browser/x")
            await conn.start()
            try:
                result = await conn.call("Page.navigate", session="S1", url="https://example.com")
                assert result == {"method": "Page.navigate", "sessionId": "S1"}
                assert fake.received[0]["sessionId"] == "S1"
                assert fake.received[0]["params"]["url"] == "https://example.com"
                assert fake.received[0]["id"] == 1
            finally:
                await conn.close()

    _run(scenario)


def test_a_protocol_error_becomes_a_cdp_error():
    async def scenario() -> None:
        fake = _FakeBrowser()
        async with websockets.serve(fake.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            conn = browser_cdp.CdpConnection(f"ws://127.0.0.1:{port}/devtools/browser/x")
            await conn.start()
            try:
                with pytest.raises(browser_cdp.CdpError, match="nope"):
                    await conn.call("Boom")
            finally:
                await conn.close()

    _run(scenario)


def test_a_silent_call_times_out():
    async def scenario() -> None:
        fake = _FakeBrowser()
        async with websockets.serve(fake.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            conn = browser_cdp.CdpConnection(f"ws://127.0.0.1:{port}/devtools/browser/x")
            await conn.start()
            try:
                with pytest.raises(browser_cdp.CdpError, match="timed out"):
                    await conn.call("Silent", timeout=0.2)
            finally:
                await conn.close()

    _run(scenario)


def test_listeners_receive_non_response_messages():
    async def scenario() -> None:
        fake = _FakeBrowser()
        async with websockets.serve(fake.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            conn = browser_cdp.CdpConnection(f"ws://127.0.0.1:{port}/devtools/browser/x")
            seen: list[dict] = []
            conn.add_listener(seen.append)
            await conn.start()
            try:
                await conn.call("Emit")
                for _ in range(50):
                    if seen:
                        break
                    await asyncio.sleep(0.02)
                assert seen and seen[0]["method"] == "Page.loadEventFired"
                conn.remove_listener(seen.append)
                assert len(conn._listeners) == 0
            finally:
                await conn.close()

    _run(scenario)


def test_a_dead_socket_fails_pending_calls():
    async def scenario() -> None:
        fake = _FakeBrowser()
        async with websockets.serve(fake.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            conn = browser_cdp.CdpConnection(f"ws://127.0.0.1:{port}/devtools/browser/x")
            await conn.start()
            pending = asyncio.create_task(conn.call("Silent", timeout=5.0))
            await asyncio.sleep(0.2)
            # The browser side goes away: the pending call must NOT hang.
            await fake.conns[0].close()
            with pytest.raises(browser_cdp.CdpError):
                await asyncio.wait_for(pending, timeout=3.0)
            with pytest.raises(browser_cdp.CdpError, match="closed"):
                await conn.call("Target.getTargets")
            await conn.close()

    _run(scenario)


def test_resolve_executable_prefers_the_explicit_path(tmp_path, monkeypatch):
    binary = tmp_path / "chrome"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    # Make the machine-specific probes miss so the explicit path is the answer.
    monkeypatch.setattr(browser_cdp, "_OPT_CHROME", str(tmp_path / "missing"))
    monkeypatch.setattr(browser_cdp, "_PLAYWRIGHT_GLOB", str(tmp_path / "none-*"))
    assert browser_cdp.resolve_executable(str(binary), ()) == str(binary)


def test_resolve_executable_rejects_a_bad_explicit_path(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_cdp, "_OPT_CHROME", str(tmp_path / "missing"))
    monkeypatch.setattr(browser_cdp, "_PLAYWRIGHT_GLOB", str(tmp_path / "none-*"))
    with pytest.raises(FileNotFoundError, match="SHERRY_BROWSER_EXECUTABLE"):
        browser_cdp.resolve_executable(str(tmp_path / "ghost"), ())


def test_resolve_executable_falls_back_to_path_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_cdp, "_OPT_CHROME", str(tmp_path / "missing"))
    monkeypatch.setattr(browser_cdp, "_PLAYWRIGHT_GLOB", str(tmp_path / "none-*"))
    monkeypatch.setattr(browser_cdp.shutil, "which", lambda name: f"/fake/bin/{name}")
    assert browser_cdp.resolve_executable("", ("chromium",)) == "/fake/bin/chromium"


def test_resolve_executable_raises_when_nothing_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_cdp, "_OPT_CHROME", str(tmp_path / "missing"))
    monkeypatch.setattr(browser_cdp, "_PLAYWRIGHT_GLOB", str(tmp_path / "none-*"))
    monkeypatch.setattr(browser_cdp.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError, match="no Chromium found"):
        browser_cdp.resolve_executable("", ("chromium",))


def test_wait_for_debug_port_reads_the_active_port_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        browser_cdp, "_read_http_json", lambda url, timeout=5.0: {"webSocketDebuggerUrl": "ws://x"}
    )
    (tmp_path / "DevToolsActivePort").write_text("9222\n/devtools/browser/abc\n")

    async def scenario():
        return await browser_cdp._wait_for_debug_port(str(tmp_path), timeout_s=1.0)

    assert asyncio.run(scenario()) == (9222, "ws://x")


def test_wait_for_debug_port_times_out_without_the_file(tmp_path):
    async def scenario():
        await browser_cdp._wait_for_debug_port(str(tmp_path), timeout_s=0.2)

    with pytest.raises(TimeoutError):
        asyncio.run(scenario())


class _FakeProc:
    def __init__(self) -> None:
        self.pid = 4242
        self.killed = False

    def kill(self) -> None:
        self.killed = True


def test_launch_retries_with_no_sandbox_on_a_host_without_userns(monkeypatch, tmp_path):
    spawned: list[list[str]] = []
    procs: list[_FakeProc] = []

    async def fake_spawn(*argv, **kwargs):
        spawned.append(list(argv))
        proc = _FakeProc()
        procs.append(proc)
        return proc

    attempts = {"n": 0}

    async def fake_wait(user_data_dir, timeout_s):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise TimeoutError("no port")
        return 9222, "ws://127.0.0.1:9222/devtools/browser/x"

    monkeypatch.setattr(browser_cdp.asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(browser_cdp, "_wait_for_debug_port", fake_wait)

    launched = asyncio.run(
        browser_cdp.launch_browser(
            executable="/x/chrome", headless=True, user_data_dir=str(tmp_path), timeout_s=1.0
        )
    )

    assert launched.no_sandbox is True
    assert launched.port == 9222
    assert len(spawned) == 2
    assert "--no-sandbox" not in spawned[0]
    assert "--no-sandbox" in spawned[1]
    assert procs[0].killed is True  # the failed first attempt was cleaned up


def test_launch_gives_up_after_the_second_attempt(monkeypatch, tmp_path):
    procs: list[_FakeProc] = []

    async def fake_spawn(*argv, **kwargs):
        proc = _FakeProc()
        procs.append(proc)
        return proc

    async def fake_wait(user_data_dir, timeout_s):
        raise TimeoutError("no port")

    monkeypatch.setattr(browser_cdp.asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(browser_cdp, "_wait_for_debug_port", fake_wait)

    with pytest.raises(TimeoutError):
        asyncio.run(
            browser_cdp.launch_browser(
                executable="/x/chrome",
                headless=True,
                user_data_dir=str(tmp_path),
                timeout_s=1.0,
            )
        )
    assert len(procs) == 2
    assert all(proc.killed for proc in procs)
