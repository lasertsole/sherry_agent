"""Task 2 tests: sandbox policy parsing, backend contract and platform dispatch.

Everything here is mocked -- no real probing (no bwrap / sandbox-exec / subprocess)
is ever executed on this Windows development machine. Platform identity is faked
via monkeypatched ``platform.system``; backends are stubbed by installing fake
modules into ``sys.modules`` (this also exercises the lazy-import contract of
``get_backend``, whose real backend modules are created by Tasks 3/4).
"""

import platform
import sys
import types
from pathlib import Path

import pytest

from agent.tools.pub_base.sandbox import (
    SandboxBackend,
    SandboxPolicy,
    get_backend,
    parse_policy,
    read_policy,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# Stub backend helpers
# --------------------------------------------------------------------------

class ProbeRecorder:
    """Mutable probe-call counter shared with stub backends."""

    def __init__(self) -> None:
        self.count: int = 0


def _install_stub_backend(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    class_name: str,
    probe_result: bool = True,
) -> tuple[type, ProbeRecorder]:
    """Install a stub backend module into sys.modules and return (class, recorder).

    The stub's ``probe`` counts every call so tests can assert that OFF never
    probes. ``wrap`` is the identity transform -- sufficient for dispatch tests.
    """
    recorder = ProbeRecorder()

    class StubBackend:
        def probe(self) -> bool:
            recorder.count += 1
            return probe_result

        def wrap(self, cmd: list[str], env: dict) -> tuple[list[str], dict]:
            return cmd, env

    stub_module = types.ModuleType(module_name)
    setattr(stub_module, class_name, StubBackend)
    monkeypatch.setitem(sys.modules, module_name, stub_module)
    return StubBackend, recorder


def _mock_platform(monkeypatch: pytest.MonkeyPatch, system: str) -> None:
    monkeypatch.setattr(platform, "system", lambda: system)


# --------------------------------------------------------------------------
# parse_policy
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("required", SandboxPolicy.REQUIRED),
    ("auto", SandboxPolicy.AUTO),
    ("off", SandboxPolicy.OFF),
])
def test_parse_policy_three_states(raw: str, expected: SandboxPolicy):
    assert parse_policy(raw) is expected


@pytest.mark.parametrize("raw,expected", [
    ("  AUTO  ", SandboxPolicy.AUTO),
    ("Off", SandboxPolicy.OFF),
    ("\tRequired\n", SandboxPolicy.REQUIRED),
    ("OFF", SandboxPolicy.OFF),
])
def test_parse_policy_strips_and_lowers(raw: str, expected: SandboxPolicy):
    assert parse_policy(raw) is expected


def test_parse_policy_none_defaults_to_auto():
    assert parse_policy(None) is SandboxPolicy.AUTO


def test_parse_policy_unknown_raises_valueerror_listing_legal_values():
    with pytest.raises(ValueError) as exc_info:
        parse_policy("bogus")
    message = str(exc_info.value)
    assert "required" in message
    assert "auto" in message
    assert "off" in message


def test_parse_policy_returns_enum_members():
    for value in ("required", "auto", "off"):
        assert isinstance(parse_policy(value), SandboxPolicy)


# --------------------------------------------------------------------------
# read_policy
# --------------------------------------------------------------------------

def test_read_policy_default_auto_when_env_unset(monkeypatch):
    monkeypatch.delenv("SANDBOX_POLICY", raising=False)
    assert read_policy() is SandboxPolicy.AUTO


def test_read_policy_reads_env_on_every_call(monkeypatch):
    """No import-time caching: flipping the env var between calls must flip the result."""
    monkeypatch.setenv("SANDBOX_POLICY", "off")
    assert read_policy() is SandboxPolicy.OFF

    monkeypatch.setenv("SANDBOX_POLICY", "required")
    assert read_policy() is SandboxPolicy.REQUIRED

    monkeypatch.setenv("SANDBOX_POLICY", " AUTO ")
    assert read_policy() is SandboxPolicy.AUTO


def test_read_policy_strips_and_lowers_env_value(monkeypatch):
    monkeypatch.setenv("SANDBOX_POLICY", "  OFF  ")
    assert read_policy() is SandboxPolicy.OFF


# --------------------------------------------------------------------------
# SandboxBackend contract
# --------------------------------------------------------------------------

def test_sandbox_backend_is_abstract():
    with pytest.raises(TypeError):
        SandboxBackend()  # type: ignore[abstract]


def test_sandbox_backend_requires_probe_and_wrap():
    class Incomplete(SandboxBackend):
        def probe(self) -> bool:
            return True

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]  # intentionally abstract (wrap missing)


# --------------------------------------------------------------------------
# get_backend: platform dispatch (all mocked)
# --------------------------------------------------------------------------

def test_get_backend_linux_returns_bwrap_backend(monkeypatch):
    _mock_platform(monkeypatch, "Linux")
    stub_cls, recorder = _install_stub_backend(
        monkeypatch, "agent.tools.pub_base.sandbox_bwrap", "BwrapBackend"
    )
    backend = get_backend(SandboxPolicy.AUTO)
    assert isinstance(backend, stub_cls)
    assert recorder.count == 1


def test_get_backend_darwin_returns_seatbelt_backend(monkeypatch):
    _mock_platform(monkeypatch, "Darwin")
    stub_cls, recorder = _install_stub_backend(
        monkeypatch, "agent.tools.pub_base.sandbox_seatbelt", "SeatbeltBackend"
    )
    backend = get_backend(SandboxPolicy.REQUIRED)
    assert isinstance(backend, stub_cls)
    assert recorder.count == 1


def test_get_backend_windows_returns_none_for_auto(monkeypatch):
    _mock_platform(monkeypatch, "Windows")
    assert get_backend(SandboxPolicy.AUTO) is None


def test_get_backend_windows_required_raises_runtimeerror(monkeypatch):
    _mock_platform(monkeypatch, "Windows")
    with pytest.raises(RuntimeError, match="Required sandbox unavailable on Windows"):
        get_backend(SandboxPolicy.REQUIRED)


# --------------------------------------------------------------------------
# get_backend: probe-failure semantics x 3 policies
# --------------------------------------------------------------------------

def test_get_backend_required_probe_fail_raises(monkeypatch):
    _mock_platform(monkeypatch, "Linux")
    _install_stub_backend(
        monkeypatch,
        "agent.tools.pub_base.sandbox_bwrap",
        "BwrapBackend",
        probe_result=False,
    )
    with pytest.raises(RuntimeError, match="Required sandbox unavailable on Linux"):
        get_backend(SandboxPolicy.REQUIRED)


def test_get_backend_auto_probe_fail_degrades_to_none(monkeypatch):
    _mock_platform(monkeypatch, "Linux")
    _install_stub_backend(
        monkeypatch,
        "agent.tools.pub_base.sandbox_bwrap",
        "BwrapBackend",
        probe_result=False,
    )
    assert get_backend(SandboxPolicy.AUTO) is None  # degrade, no exception


def test_get_backend_off_skips_probe_entirely(monkeypatch):
    _mock_platform(monkeypatch, "Linux")
    _install_stub_backend(
        monkeypatch,
        "agent.tools.pub_base.sandbox_bwrap",
        "BwrapBackend",
        probe_result=False,
    )
    assert get_backend(SandboxPolicy.OFF) is None


def test_get_backend_off_never_calls_probe(monkeypatch):
    _mock_platform(monkeypatch, "Linux")
    _, recorder = _install_stub_backend(
        monkeypatch, "agent.tools.pub_base.sandbox_bwrap", "BwrapBackend"
    )
    get_backend(SandboxPolicy.OFF)
    assert recorder.count == 0


# --------------------------------------------------------------------------
# get_backend: lazy-import robustness (backend modules may not exist yet)
# --------------------------------------------------------------------------

def test_get_backend_import_error_treated_as_unavailable_auto(monkeypatch):
    """sysmodules entry None => 'from x import y' raises ImportError; AUTO must
    degrade to None instead of crashing."""
    _mock_platform(monkeypatch, "Linux")
    monkeypatch.setitem(sys.modules, "agent.tools.pub_base.sandbox_bwrap", None)
    assert get_backend(SandboxPolicy.AUTO) is None


def test_get_backend_import_error_required_raises(monkeypatch):
    _mock_platform(monkeypatch, "Linux")
    monkeypatch.setitem(sys.modules, "agent.tools.pub_base.sandbox_bwrap", None)
    with pytest.raises(RuntimeError, match="Required sandbox unavailable on Linux"):
        get_backend(SandboxPolicy.REQUIRED)


# --------------------------------------------------------------------------
# .env.example documentation
# --------------------------------------------------------------------------

def test_env_example_contains_sandbox_policy_line():
    content = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    matching = [
        line for line in content.splitlines()
        if line.strip().startswith("SANDBOX_POLICY")
    ]
    assert matching, "SANDBOX_POLICY missing from .env.example"
    line = matching[-1].strip()
    assert line.replace(" ", "") == "SANDBOX_POLICY=auto"
    # semantics documented in the comment block
    lowered = content.lower()
    assert "required" in lowered and "auto" in lowered and "off" in lowered
