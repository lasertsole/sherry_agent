"""Integration tests for PTC runner hardening: token lifecycle + sandbox wrap.

The child spawn is replaced by a capturing fake process, so these exercise the
real ``run_ptc`` orchestration (server, temp dir, stub, env) without paying a
process launch per case.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import contextmanager
from typing import Any

import pytest
from loguru import logger

from agent.tools.ptc import runner
from agent.tools.ptc.stub_generator import PTC_RPC_TOKEN_ENV
from config.features import PTC

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


@contextmanager
def _loguru_warnings():
    records: list[str] = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        yield records
    finally:
        logger.remove(sink_id)


class _FakeProc:
    """Stand-in for a spawned child on the fd-direct-write path.

    stdout/stderr are FILE handles now (never pipes): the fake writes its
    envelope into ``kwargs["stdout"]`` at construction and the runner waits
    with ``wait(timeout)``. ``poll`` serves the cleanup guard.
    """

    returncode = 0

    def __init__(self, *args: Any, **kwargs: Any):
        handle = kwargs.get("stdout")
        if hasattr(handle, "write"):
            handle.write(b'{"out": "", "err": "", "exc": null}')

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:  # pragma: no cover
        raise AssertionError("the fd-direct-write path must not drain a pipe")

    def poll(self) -> int:
        return 0

    def kill(self) -> None:
        pass


@contextmanager
def _capture_spawn(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    def _fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["env"] = kwargs.get("env")
        stub_path = os.path.join(kwargs["env"]["PYTHONPATH"], "sherry_tools.py")
        with open(stub_path, encoding="utf-8") as handle:
            captured["stub"] = handle.read()
        return _FakeProc(**kwargs)

    monkeypatch.setattr(runner.subprocess, "Popen", _fake_popen)
    yield captured


def _run_spy(code: str, config: dict, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    with _capture_spawn(monkeypatch) as captured:
        raw = asyncio.run(runner.run_ptc(code, {}, "child:sess", config))
    captured["result"] = json.loads(raw)
    return captured


class _NoopBackend:
    """A usable backend double: the child still spawns (wrapped unchanged)."""

    def wrap(
        self,
        cmd: list[str],
        env: dict,
        writable_roots: list | None = None,
        *,
        allow_network: bool = False,
    ) -> tuple[list[str], dict]:
        return list(cmd), env


def test_child_env_carries_token_but_stub_file_never_contains_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "get_backend", lambda _policy: _NoopBackend())
    captured = _run_spy("print('x')", dict(PTC), monkeypatch)
    assert captured["result"]["status"] == "ok"
    token = captured["env"][PTC_RPC_TOKEN_ENV]
    assert token
    # The stub reads the env var by name, but the token value is never on disk.
    assert PTC_RPC_TOKEN_ENV in captured["stub"]
    assert token not in captured["stub"]


def test_token_is_unique_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "get_backend", lambda _policy: _NoopBackend())
    first = _run_spy("print('x')", dict(PTC), monkeypatch)
    second = _run_spy("print('x')", dict(PTC), monkeypatch)
    first_token = first["env"][PTC_RPC_TOKEN_ENV]
    second_token = second["env"][PTC_RPC_TOKEN_ENV]
    assert first_token and second_token
    assert first_token != second_token


def test_available_backend_wraps_the_child_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    wrap_calls: list[list[str]] = []

    class _Backend:
        def wrap(
            self,
            cmd: list[str],
            env: dict,
            writable_roots: list | None = None,
            *,
            allow_network: bool = False,
        ) -> tuple[list[str], dict]:
            wrap_calls.append(list(cmd))
            return ["/fake/bwrap", "--", *cmd], env

    monkeypatch.setattr(runner, "get_backend", lambda _policy: _Backend())
    captured = _run_spy("print('x')", dict(PTC), monkeypatch)
    argv = captured["argv"]
    assert argv[:2] == ["/fake/bwrap", "--"]
    assert argv[2] == sys.executable
    assert argv[-1].endswith("script.py")
    assert wrap_calls == [[sys.executable, argv[-1]]]


def test_auto_without_backend_refuses_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTO + no backend is a refusal envelope for PTC, not a bare run."""
    monkeypatch.setattr(runner, "get_backend", lambda _policy: None)

    def _no_spawn(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("PTC must not spawn without an OS sandbox backend")

    monkeypatch.setattr(runner.subprocess, "Popen", _no_spawn)
    raw = asyncio.run(runner.run_ptc("print('x')", {}, "child:sess", dict(PTC)))
    result = json.loads(raw)

    assert result["status"] == "sandbox_unavailable"
    assert "SANDBOX_POLICY=auto" in result["error"]
    assert "no operator approval path" in result["error"]


class TestGrandchildDoesNotHang:
    """The runner waits on the DIRECT child only (stdout is a file, not a pipe)."""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="POSIX job control only")
    @pytest.mark.timeout(60)
    async def test_a_backgrounded_grandchild_returns_immediately(self, tmp_path, monkeypatch):
        import time

        from agent.tools.ptc import runner as ptc_runner

        monkeypatch.setattr(
            ptc_runner,
            "_resolve_sandboxed_argv",
            lambda argv, env, roots, **kwargs: (argv, env),
        )

        from config.features import PTC

        started = time.monotonic()
        result = await ptc_runner.run_ptc(
            "import os; os.system('sleep 3 & print(\"quick\", flush=True)')",
            {},
            "s-ptc-hang",
            PTC,
        )
        elapsed = time.monotonic() - started

        assert elapsed < 2.5, f"the runner must not wait for the grandchild ({elapsed:.1f}s)"
        payload = json.loads(result)
        assert payload.get("status") in {"ok", "error"}
