"""PTC test fixtures.

AUTO + no OS backend now REFUSES a PTC run (H3: PTC has no HITL path), so the
runner tests would all answer ``sandbox_unavailable`` on a CI host without
bwrap. This fixture provides a usable backend double for the mechanics-focused
suites; the sandbox-policy tests override ``runner.get_backend`` afterwards
(their monkeypatch lands later and wins).
"""

from __future__ import annotations

import pytest

from agent.tools.ptc import runner


class _NoopBackend:
    def wrap(
        self,
        cmd: list[str],
        env: dict,
        writable_roots: list | None = None,
        *,
        allow_network: bool = False,
    ) -> tuple[list[str], dict]:
        return list(cmd), env


@pytest.fixture(autouse=True)
def _usable_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "get_backend", lambda _policy: _NoopBackend())
