"""H3 predicates: when does a requested sandbox actually run / degrade?"""

from __future__ import annotations

import pytest

import agent.tools.pub_base.sandbox as sandbox
from agent.tools.pub_base.sandbox import SandboxPolicy

# Captured before the global conftest fixture replaces the module attributes.
_REAL_EFFECTIVE = sandbox.sandbox_effective
_REAL_DEGRADED = sandbox.sandbox_degraded


@pytest.fixture(autouse=True)
def _use_the_real_predicates(monkeypatch):
    """Undo the suite-wide usable-sandbox assumption for these unit tests."""
    monkeypatch.setattr(sandbox, "sandbox_effective", _REAL_EFFECTIVE)
    monkeypatch.setattr(sandbox, "sandbox_degraded", _REAL_DEGRADED)


class _Backend:
    def probe(self) -> bool:
        return True


@pytest.fixture()
def knobs(monkeypatch):
    """Control read_policy/get_backend independently of the host."""

    def set_knobs(policy, backend):
        monkeypatch.setattr(sandbox, "read_policy", lambda: policy)
        monkeypatch.setattr(sandbox, "get_backend", lambda _p: backend)

    return set_knobs


def test_not_requested_is_neither_effective_nor_degraded(knobs):
    knobs(SandboxPolicy.AUTO, _Backend())
    assert _REAL_EFFECTIVE(False) is False
    assert _REAL_DEGRADED(False) is False


def test_off_is_not_effective_and_not_degraded(knobs):
    knobs(SandboxPolicy.OFF, None)
    assert _REAL_EFFECTIVE(True) is False
    assert _REAL_DEGRADED(True) is False


def test_auto_with_a_backend_is_effective(knobs):
    knobs(SandboxPolicy.AUTO, _Backend())
    assert _REAL_EFFECTIVE(True) is True
    assert _REAL_DEGRADED(True) is False


def test_auto_without_a_backend_is_degraded(knobs):
    knobs(SandboxPolicy.AUTO, None)
    assert _REAL_EFFECTIVE(True) is False
    assert _REAL_DEGRADED(True) is True


def test_required_without_a_backend_is_not_degraded(monkeypatch):
    def raising(_policy):
        raise RuntimeError("Required sandbox unavailable")

    monkeypatch.setattr(sandbox, "read_policy", lambda: SandboxPolicy.REQUIRED)
    monkeypatch.setattr(sandbox, "get_backend", raising)
    # sandbox_degraded only covers AUTO; REQUIRED is the tool layer's refusal.
    assert _REAL_EFFECTIVE(True) is False
    assert _REAL_DEGRADED(True) is False
