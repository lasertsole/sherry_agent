"""Shared HITL test assumptions.

Most of these suites predate the sandbox-degradation fix and were written for a host WITH a
usable OS sandbox: they assert that ``sandbox=True`` calls never reach the
bypass approval. The sandbox-degradation path (AUTO + no backend) is exercised
explicitly in ``test_hitl_sandbox_degradation.py``, which overrides this
fixture per test.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _assume_a_usable_os_sandbox(monkeypatch: pytest.MonkeyPatch):
    import agent.tools.pub_base.sandbox as sandbox

    monkeypatch.setattr(sandbox, "sandbox_degraded", lambda requested=True: False)
    monkeypatch.setattr(sandbox, "sandbox_effective", lambda requested=True: bool(requested))
