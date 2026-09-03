"""Tests for the sandboxed python_repl tool (sandbox-hardening Task 7).

Covers the GREEN spec of `.omo/plans/sandbox-hardening.md` lines 647-731:

- schema auto-derivation: PythonREPLTool has NO explicit ``args_schema`` (the
  OPPOSITE mechanism from terminal's ShellTool), so ``tool_call_schema``
  derives from the ``_run`` signature — adding ``sandbox: bool = True`` to
  ``_run`` must surface it in the schema with default True (no pydantic Field).
- env scrub: Popen gains ``env=scrub_env()`` (no secret vars survive).
- cwd clamp: Popen gains ``cwd=str(ROOT_DIR)``.
- sandbox wrap: sandbox=True + backend present → ``backend.wrap([sys.executable,
  "-c", script], env)`` list exec form; backend None → direct list + env/cwd.
- scope guard: caller_scope != "main" + sandbox=False → ToolException;
  REQUIRED policy + sandbox=False → ToolException.
- async passthrough: ``_arun`` forwards sandbox the same way.
"""

from __future__ import annotations

from typing import Any

import asyncio
import json
import sys
import unittest.mock

import pytest
from config.path import ROOT_DIR
from langchain_core.tools import ToolException

import agent.tools.python_repl as python_repl
from agent.tools.python_repl import TimedPythonREPLTool, build_python_repl_tool
from agent.tools.pub_base.sandbox import SandboxPolicy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OK_PAYLOAD = json.dumps({"out": "2\n", "err": "", "exc": None, "tb": None})


class _FakeProc:
    """Minimal Popen stand-in returning a valid _REPL_WRAPPER payload."""

    _calls: list[dict[str, Any]]
    returncode: int

    def __init__(self, calls: list[dict[str, Any]], *args: object, **kwargs: object):
        self._calls = calls
        calls.append({"args": args, "kwargs": kwargs})
        self.returncode = 0

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return _OK_PAYLOAD, ""

    def kill(self) -> None:
        pass


def _install_fake_popen(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch subprocess.Popen used by python_repl; return the call record list."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        python_repl.subprocess, "Popen", lambda *a, **kw: _FakeProc(calls, *a, **kw)
    )
    return calls


class _FakeBackend:
    """SandboxBackend-shaped fake recording wrap() inputs."""

    wrap_inputs: list[dict[str, Any]]

    def __init__(self) -> None:
        self.wrap_inputs = []

    def probe(self) -> bool:  # pragma: no cover - contract completeness only
        return True

    def wrap(self, cmd: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        self.wrap_inputs.append({"cmd": cmd, "env": env})
        wrapped_env = dict(env)
        wrapped_env["SANDBOX_FAKE"] = "1"
        return (["fake-sandbox-runner", "--"] + cmd, wrapped_env)


# ---------------------------------------------------------------------------
# Schema auto-derivation (the OPPOSITE mechanism from terminal's explicit
# args_schema subclassing): PythonREPLTool defines no args_schema, so
# tool_call_schema is derived from the _run signature.
# ---------------------------------------------------------------------------


def test_schema_auto_derives_sandbox_with_default_true():
    tool = build_python_repl_tool()
    props = tool.tool_call_schema.model_json_schema()["properties"]
    # Proves the schema picked the plain _run parameter up (no pydantic Field).
    assert "sandbox" in props
    assert props["sandbox"].get("default") is True
    assert props["sandbox"].get("type") == "boolean"
    # Still the stdin-feed protocol's single code arg plus the new flag.
    assert set(props) == {"query", "sandbox"}


def test_class_level_tool_call_schema_access():
    # Plan acceptance one-liner (sandbox-hardening.md line 694) reads the
    # schema from the CLASS; the descriptor must support that too.
    props = TimedPythonREPLTool.tool_call_schema.model_json_schema()["properties"]
    assert "sandbox" in props


def test_schema_has_no_explicit_args_schema():
    # Auto-derivation proof: the class chain must NOT carry an explicit
    # args_schema (that would be terminal's mechanism, not this one).
    assert getattr(TimedPythonREPLTool, "args_schema", None) is None


# ---------------------------------------------------------------------------
# Env scrub + cwd clamp on the Popen spawn point
# ---------------------------------------------------------------------------


def test_popen_gets_scrubbed_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAIN_LLM_API_KEY", "sk-secret")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret")
    monkeypatch.setenv("SHERRY_BENIGN_VAR_XYZ", "keep-me")
    calls = _install_fake_popen(monkeypatch)

    tool = build_python_repl_tool()
    tool.run({"query": "print(1+1)"})

    assert len(calls) == 1
    env = calls[0]["kwargs"]["env"]
    assert env is not None
    assert "MAIN_LLM_API_KEY" not in env
    assert "TAVILY_API_KEY" not in env
    assert env["SHERRY_BENIGN_VAR_XYZ"] == "keep-me"
    assert "PATH" in env  # keep-by-name rule keeps the child functional


def test_popen_cwd_clamped_to_root_dir(monkeypatch: pytest.MonkeyPatch):
    calls = _install_fake_popen(monkeypatch)

    tool = build_python_repl_tool()
    tool.run({"query": "print(1+1)"})

    assert calls[0]["kwargs"]["cwd"] == str(ROOT_DIR)


# ---------------------------------------------------------------------------
# Sandbox wrap path (list exec form) vs backend-None direct path
# ---------------------------------------------------------------------------


def test_backend_wrap_path_uses_list_exec(monkeypatch: pytest.MonkeyPatch):
    calls = _install_fake_popen(monkeypatch)
    backend = _FakeBackend()
    monkeypatch.setattr(python_repl, "get_backend", lambda policy: backend)
    monkeypatch.setattr(
        python_repl, "read_policy", lambda: SandboxPolicy.AUTO
    )

    tool = build_python_repl_tool()
    tool.run({"query": "print(1+1)"})

    # backend.wrap received the interpreter argv (list, not string).
    assert len(backend.wrap_inputs) == 1
    assert backend.wrap_inputs[0]["cmd"][0] == sys.executable
    assert backend.wrap_inputs[0]["cmd"][1] == "-c"

    # Popen first arg IS the wrapped LIST (never a string).
    assert len(calls) == 1
    argv = calls[0]["args"][0]
    assert isinstance(argv, list)
    assert argv == ["fake-sandbox-runner", "--", sys.executable, "-c", argv[4]]
    assert argv[4].startswith("import sys, json, traceback")  # _REPL_WRAPPER intact
    # wrap's env mutation flows into Popen.
    assert calls[0]["kwargs"]["env"]["SANDBOX_FAKE"] == "1"
    assert calls[0]["kwargs"]["cwd"] == str(ROOT_DIR)


def test_backend_none_keeps_direct_list_with_env_cwd(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = _install_fake_popen(monkeypatch)
    monkeypatch.setattr(python_repl, "get_backend", lambda policy: None)
    monkeypatch.setattr(python_repl, "read_policy", lambda: SandboxPolicy.AUTO)

    tool = build_python_repl_tool()
    tool.run({"query": "print(1+1)"})

    assert len(calls) == 1
    argv = calls[0]["args"][0]
    assert isinstance(argv, list)
    assert argv[0] == sys.executable
    assert argv[1] == "-c"
    assert argv[2].startswith("import sys, json, traceback")  # _REPL_WRAPPER intact
    assert calls[0]["kwargs"]["env"] is not None
    assert calls[0]["kwargs"]["cwd"] == str(ROOT_DIR)


def test_policy_off_never_touches_backend(monkeypatch: pytest.MonkeyPatch):
    calls = _install_fake_popen(monkeypatch)

    def _forbidden(policy):
        raise AssertionError("get_backend must not be called under policy=off")

    monkeypatch.setattr(python_repl, "get_backend", _forbidden)
    monkeypatch.setattr(python_repl, "read_policy", lambda: SandboxPolicy.OFF)

    tool = build_python_repl_tool()
    tool.run({"query": "print(1+1)"})

    assert len(calls) == 1
    assert isinstance(calls[0]["args"][0], list)


def test_auto_degrade_emits_one_loguru_warning(monkeypatch: pytest.MonkeyPatch):
    _install_fake_popen(monkeypatch)
    monkeypatch.setattr(python_repl, "get_backend", lambda policy: None)
    monkeypatch.setattr(python_repl, "read_policy", lambda: SandboxPolicy.AUTO)

    records: list[str] = []
    sink_id = python_repl.logger.add(records.append, level="WARNING")
    try:
        tool = build_python_repl_tool()
        tool.run({"query": "print(1+1)"})
    finally:
        python_repl.logger.remove(sink_id)

    degrade_lines = [r for r in records if "sandbox" in r and "degrad" in r]
    assert len(degrade_lines) == 1


# ---------------------------------------------------------------------------
# Scope / policy guards
# ---------------------------------------------------------------------------


def _subagent_tool() -> TimedPythonREPLTool:
    tool = build_python_repl_tool()
    tool.metadata = {"idempotent": False, "caller_scope": "subagent"}
    return tool


def test_scope_deny_subagent_sandbox_false():
    tool = _subagent_tool()
    with pytest.raises(ToolException) as excinfo:
        tool._run("print(1)", sandbox=False)
    msg = str(excinfo.value)
    assert "主会话" in msg or "main" in msg


def test_scope_deny_main_sandbox_false_allowed(monkeypatch: pytest.MonkeyPatch):
    calls = _install_fake_popen(monkeypatch)
    monkeypatch.setattr(python_repl, "get_backend", lambda policy: None)
    monkeypatch.setattr(python_repl, "read_policy", lambda: SandboxPolicy.AUTO)

    tool = build_python_repl_tool()  # default metadata → caller_scope=main
    out = tool._run("print(1)", sandbox=False)
    assert out == "2\n"
    assert len(calls) == 1


def test_required_policy_deny_sandbox_false(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        python_repl, "read_policy", lambda: SandboxPolicy.REQUIRED
    )
    tool = build_python_repl_tool()  # main scope, but REQUIRED denies anyway
    with pytest.raises(ToolException) as excinfo:
        tool._run("print(1)", sandbox=False)
    msg = str(excinfo.value)
    assert "主会话" in msg or "main" in msg


def test_scope_subagent_sandbox_true_unaffected(monkeypatch: pytest.MonkeyPatch):
    calls = _install_fake_popen(monkeypatch)
    monkeypatch.setattr(python_repl, "get_backend", lambda policy: None)
    monkeypatch.setattr(python_repl, "read_policy", lambda: SandboxPolicy.AUTO)

    tool = _subagent_tool()
    out = tool._run("print(1)", sandbox=True)
    assert out == "2\n"
    assert len(calls) == 1


def test_scope_deny_via_public_run(monkeypatch: pytest.MonkeyPatch):
    # handle_tool_error=True (langchain_core 1.4.7) returns the exception
    # message as a plain string — the deny must surface, never raise.
    _install_fake_popen(monkeypatch)
    monkeypatch.setattr(
        python_repl, "read_policy", lambda: SandboxPolicy.REQUIRED
    )
    tool = build_python_repl_tool()
    out = tool.run({"query": "print(1)", "sandbox": False})
    assert "主会话" in out or "main" in out


# ---------------------------------------------------------------------------
# Async passthrough
# ---------------------------------------------------------------------------


def test_arun_supports_sandbox_passthrough(monkeypatch: pytest.MonkeyPatch):
    calls = _install_fake_popen(monkeypatch)
    monkeypatch.setattr(python_repl, "get_backend", lambda policy: None)
    monkeypatch.setattr(python_repl, "read_policy", lambda: SandboxPolicy.AUTO)

    tool = build_python_repl_tool()
    out = asyncio.run(tool._arun("print(1)", sandbox=True))
    assert out == "2\n"
    assert len(calls) == 1


def test_arun_scope_guard_applies(monkeypatch: pytest.MonkeyPatch):
    _install_fake_popen(monkeypatch)
    monkeypatch.setattr(python_repl, "get_backend", lambda policy: None)
    monkeypatch.setattr(python_repl, "read_policy", lambda: SandboxPolicy.AUTO)

    tool = _subagent_tool()
    with pytest.raises(ToolException):
        asyncio.run(tool._arun("print(1)", sandbox=False))


# ---------------------------------------------------------------------------
# Real-exec happy path: proves the _REPL_WRAPPER stdin-feed protocol (well,
# -c argv feed) survives the rework. Spawns one real python subprocess.
# ---------------------------------------------------------------------------


def test_real_exec_wrapper_protocol_intact():
    tool = build_python_repl_tool()
    out = tool.run({"query": "print(1+1)"})
    assert "2" in out


def test_run_with_timeout_absorbs_unexpected_kwargs():
    # The plan's **kwargs placeholder absorbs extras silently — pin that
    # contract so future callers know it does not raise.
    calls = []
    proc = _FakeProc(calls, [sys.executable, "-c", "pass"])
    with unittest.mock.patch.object(
        python_repl.subprocess, "Popen", return_value=proc
    ):
        out = python_repl._run_with_timeout("pass", 5, True, future_flag="x")
    assert isinstance(out, str)
