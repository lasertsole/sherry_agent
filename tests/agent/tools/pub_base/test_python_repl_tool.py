"""Tests for the sandboxed python_repl tool (sandbox-hardening Task 7).

Covers the sandbox-hardening spec:

- model-facing schema: ``PythonReplInput`` exposes ``query`` ONLY. The internal
  ``sandbox`` parameter stays on ``_run``/``_arun`` for server-side callers but
  is deliberately not model-visible (2026-11-09 audit H2).
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
import os
import sys
import unittest.mock

import pytest
from config.path import ROOT_DIR
from langchain_core.tools import ToolException

import agent.tools.pub_base.sandbox_guard as sandbox_guard
import agent.tools.python_repl as python_repl
from agent.tools.python_repl import TimedPythonREPLTool, build_python_repl_tool


def _set_policy(monkeypatch, policy):
    """Set the policy at BOTH consumption points: the SandboxGuardMixin guard
    and python_repl's own _resolve_sandbox_argv read."""
    monkeypatch.setattr(sandbox_guard, "read_policy", lambda: policy)
    monkeypatch.setattr(python_repl, "read_policy", lambda: policy)


from agent.tools.pub_base.sandbox import SandboxPolicy


@pytest.fixture(autouse=True)
def _default_policy_auto(monkeypatch: pytest.MonkeyPatch):
    """Hermetic default policy for this file.

    The shipped (tracked) ``.env`` sets ``SANDBOX_POLICY=auto``; a deployment can
    set ``required``, which denies the main-agent ``sandbox=False`` bypass several
    tests here exercise. Tests that assert the REQUIRED behaviour call
    ``_set_policy`` themselves — that patch lands after this one and wins.
    """
    _set_policy(monkeypatch, SandboxPolicy.AUTO)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_OK_PAYLOAD = json.dumps({"out": "2\n", "err": "", "exc": None, "tb": None})


class _FakeProc:
    """Minimal Popen stand-in writing a valid _REPL_WRAPPER payload into stdout.

    The runner is fd-direct-write now: stdout/stderr are FILE handles (never
    pipes) and the flow waits with ``wait()`` — the fake therefore emits its
    envelope through ``kwargs["stdout"]`` at construction (before the parent
    closes its own handle) and serves ``wait()``.
    """

    _calls: list[dict[str, Any]]
    returncode: int

    def __init__(self, calls: list[dict[str, Any]], *args: object, **kwargs: object):
        self._calls = calls
        calls.append({"args": args, "kwargs": kwargs})
        self.returncode = 0
        handle = kwargs.get("stdout")
        if hasattr(handle, "write"):
            handle.write(_OK_PAYLOAD.encode("utf-8"))

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:  # pragma: no cover
        raise AssertionError("the fd-direct-write path must not drain a pipe")

    def kill(self) -> None:
        pass


def _install_fake_popen(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch subprocess.Popen used by python_repl; return the call record list.

    Warms the sandbox probe cache with the REAL subprocess first: on hosts
    where bwrap is usable, get_backend() issues a probe Popen call (env =
    full os.environ) that would otherwise be recorded by the fake below and
    break the ``len(calls) == 1`` assertions. The probe result is cached for
    the process lifetime, so the warm-up makes every test environment-agnostic.
    """
    from agent.tools.pub_base.sandbox import SandboxPolicy, get_backend

    get_backend(SandboxPolicy.AUTO)

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

    def wrap(
        self, cmd: list[str], env: dict[str, str], writable_roots: list | None = None
    ) -> tuple[list[str], dict[str, str]]:
        self.wrap_inputs.append({"cmd": cmd, "env": env})
        wrapped_env = dict(env)
        wrapped_env["SANDBOX_FAKE"] = "1"
        return (["fake-sandbox-runner", "--"] + cmd, wrapped_env)


# ---------------------------------------------------------------------------
# Schema auto-derivation (the OPPOSITE mechanism from terminal's explicit
# args_schema subclassing): PythonREPLTool defines no args_schema, so
# tool_call_schema is derived from the _run signature.
# ---------------------------------------------------------------------------


pytestmark = [pytest.mark.module]


def test_schema_is_query_only_and_hides_the_sandbox_switch():
    tool = build_python_repl_tool()
    props = tool.tool_call_schema.model_json_schema()["properties"]
    # H2 (2026-11-09 audit): the sandbox is server-owned policy, so the model
    # sees only the code argument.
    assert set(props) == {"query"}


def test_class_level_tool_call_schema_access():
    # Plan acceptance one-liner (sandbox-hardening.md line 694) reads the
    # schema from the CLASS; the descriptor must support that too.
    props = TimedPythonREPLTool.tool_call_schema.model_json_schema()["properties"]
    assert set(props) == {"query"}


def test_schema_is_explicit_query_only_input():
    # The explicit args_schema is what keeps the internal sandbox parameter out.
    from agent.tools.python_repl import PythonReplInput

    assert TimedPythonREPLTool.model_fields["args_schema"].default is PythonReplInput
    assert set(PythonReplInput.model_fields) == {"query"}


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
    _set_policy(monkeypatch, SandboxPolicy.AUTO)

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
    _set_policy(monkeypatch, SandboxPolicy.AUTO)

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
    _set_policy(monkeypatch, SandboxPolicy.OFF)

    tool = build_python_repl_tool()
    tool.run({"query": "print(1+1)"})

    assert len(calls) == 1
    assert isinstance(calls[0]["args"][0], list)


def test_auto_degrade_emits_one_loguru_info_line(monkeypatch: pytest.MonkeyPatch):
    _install_fake_popen(monkeypatch)
    monkeypatch.setattr(python_repl, "get_backend", lambda policy: None)
    _set_policy(monkeypatch, SandboxPolicy.AUTO)

    records: list[str] = []
    sink_id = python_repl.logger.add(records.append, level="INFO")
    try:
        tool = build_python_repl_tool()
        tool.run({"query": "print(1+1)"})
    finally:
        python_repl.logger.remove(sink_id)

    degrade_lines = [r for r in records if "sandbox unavailable" in r]
    assert len(degrade_lines) == 1
    assert "unsandboxed" in degrade_lines[0]


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
    assert "main-session" in msg or "main" in msg


def test_scope_deny_main_sandbox_false_allowed(monkeypatch: pytest.MonkeyPatch):
    calls = _install_fake_popen(monkeypatch)
    monkeypatch.setattr(python_repl, "get_backend", lambda policy: None)
    _set_policy(monkeypatch, SandboxPolicy.AUTO)

    tool = build_python_repl_tool()  # default metadata → caller_scope=main
    out = tool._run("print(1)", sandbox=False)
    assert out == "2\n"
    assert len(calls) == 1


def test_required_policy_deny_sandbox_false(monkeypatch: pytest.MonkeyPatch):
    _set_policy(monkeypatch, SandboxPolicy.REQUIRED)
    tool = build_python_repl_tool()  # main scope, but REQUIRED denies anyway
    with pytest.raises(ToolException) as excinfo:
        tool._run("print(1)", sandbox=False)
    msg = str(excinfo.value)
    assert "main-session" in msg or "main" in msg


def test_subagent_sandbox_true_without_a_backend_is_refused(
    monkeypatch: pytest.MonkeyPatch,
):
    """H3: a subagent has no HITL card, so the AUTO degrade must refuse."""
    calls = _install_fake_popen(monkeypatch)
    monkeypatch.setattr(python_repl, "get_backend", lambda policy: None)
    import agent.tools.pub_base.sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod, "sandbox_effective", lambda requested=True: False)
    _set_policy(monkeypatch, SandboxPolicy.AUTO)
    tool = _subagent_tool()

    with pytest.raises(ToolException) as excinfo:
        tool._run("print(1)", sandbox=True)

    message = str(excinfo.value)
    assert "no operator approval path" in message
    assert "subagent" in message
    assert calls == [], "a refused degrade must not spawn"


def test_main_sandbox_true_without_a_backend_still_degrades(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = _install_fake_popen(monkeypatch)
    monkeypatch.setattr(python_repl, "get_backend", lambda policy: None)
    import agent.tools.pub_base.sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod, "sandbox_effective", lambda requested=True: False)
    _set_policy(monkeypatch, SandboxPolicy.AUTO)
    tool = build_python_repl_tool()

    out = tool._run("print(1)", sandbox=True)

    assert out == "2\n"
    assert len(calls) == 1


def test_scope_subagent_sandbox_true_unaffected(monkeypatch: pytest.MonkeyPatch):
    calls = _install_fake_popen(monkeypatch)
    monkeypatch.setattr(python_repl, "get_backend", lambda policy: None)
    _set_policy(monkeypatch, SandboxPolicy.AUTO)

    tool = _subagent_tool()
    out = tool._run("print(1)", sandbox=True)
    assert out == "2\n"
    assert len(calls) == 1


def test_public_run_discards_model_supplied_sandbox_false(monkeypatch: pytest.MonkeyPatch):
    # H2 (2026-11-09 audit): the model cannot reach the internal switch. A
    # forged ``sandbox=false`` is dropped by the query-only args_schema, so the
    # run stays on the sandboxed/default path.
    calls = _install_fake_popen(monkeypatch)
    monkeypatch.setattr(python_repl, "get_backend", lambda policy: None)
    _set_policy(monkeypatch, SandboxPolicy.AUTO)

    tool = build_python_repl_tool()
    out = tool.run({"query": "print(1)", "sandbox": False})

    assert out == "2\n"
    assert len(calls) == 1


def test_scope_deny_still_applies_to_internal_callers(monkeypatch: pytest.MonkeyPatch):
    # The server-side parameter keeps its guard (tool layer, not HITL).
    _install_fake_popen(monkeypatch)
    _set_policy(monkeypatch, SandboxPolicy.REQUIRED)
    tool = build_python_repl_tool()
    with pytest.raises(ToolException):
        tool._run("print(1)", sandbox=False)


# ---------------------------------------------------------------------------
# Async passthrough
# ---------------------------------------------------------------------------


def test_arun_supports_sandbox_passthrough(monkeypatch: pytest.MonkeyPatch):
    calls = _install_fake_popen(monkeypatch)
    monkeypatch.setattr(python_repl, "get_backend", lambda policy: None)
    _set_policy(monkeypatch, SandboxPolicy.AUTO)

    tool = build_python_repl_tool()
    out = asyncio.run(tool._arun("print(1)", sandbox=True))
    assert out == "2\n"
    assert len(calls) == 1


def test_arun_scope_guard_applies(monkeypatch: pytest.MonkeyPatch):
    _install_fake_popen(monkeypatch)
    monkeypatch.setattr(python_repl, "get_backend", lambda policy: None)
    _set_policy(monkeypatch, SandboxPolicy.AUTO)

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
    with unittest.mock.patch.object(python_repl.subprocess, "Popen", return_value=proc):
        out = python_repl._run_with_timeout("pass", 5, True, future_flag="x")
    assert isinstance(out, tuple) and isinstance(out[0], str)


# ---------------------------------------------------------------------------
# Sensitive-file assessment: no deny-read regex is added for python_repl because the
# restricted builtins already exclude `open` / `__import__` — lock that barrier
# so a future builtins-dict broadening cannot silently open the read path.
# ---------------------------------------------------------------------------


def test_open_is_unavailable_in_restricted_builtins():
    out, _sandboxed = python_repl._run_with_timeout('open("/etc/passwd")', 10, sandbox=False)
    assert "NameError" in out
    assert "open" in out


def test_import_is_unavailable_in_restricted_builtins():
    out, _sandboxed = python_repl._run_with_timeout('__import__("os").getcwd()', 10, sandbox=False)
    assert "NameError" in out
    assert "__import__" in out


def test_benign_repl_code_still_executes():
    out, _sandboxed = python_repl._run_with_timeout("print(sum(range(5)))", 10, sandbox=False)
    assert out.strip() == "10"


# 2026-11-09 audit H4: the builtins set alone cannot stop class introspection.
def test_class_introspection_escape_refused_before_spawn(monkeypatch: pytest.MonkeyPatch):
    calls = _install_fake_popen(monkeypatch)

    out, ran_sandboxed = python_repl._run_with_timeout(
        "print(().__class__.__base__.__subclasses__())", 10, sandbox=False
    )

    assert "refused" in out
    assert "not allowed in restricted code" in out
    assert "attribute access" in out
    assert ran_sandboxed is False
    assert calls == [], "a refused script must never spawn a child"


def test_introspection_helpers_removed_from_builtins():
    snippets = {
        "getattr": "getattr(1, 'real')",
        "type": "type(1)",
        "dir": "dir()",
        "vars": "vars()",
    }
    for helper, snippet in snippets.items():
        out, _sandboxed = python_repl._run_with_timeout(snippet, 10, sandbox=False)
        assert "NameError" in out, f"{helper} must be absent, got: {out}"
        assert helper in out


# ---------------------------------------------------------------------------
# Anti-hang: a backgrounded grandchild must not hold the tool
# ---------------------------------------------------------------------------


class TestGrandchildDoesNotHang:
    @pytest.mark.integration
    @pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX job control only")
    @pytest.mark.timeout(30)
    def test_a_backgrounded_grandchild_returns_immediately(self):
        """The child backgrounds a grandchild that outlives it.

        With pipes this blocked: ``communicate()`` waits for EOF, and the
        grandchild inherited the write end, so the call would hang until the
        grandchild exited. stdout is a FILE now, so only the direct child's exit
        matters — the tool returns while the grandchild still sleeps.
        """
        import time

        from agent.tools.python_repl import build_python_repl_tool

        tool = build_python_repl_tool()
        started = time.monotonic()
        result = tool._run(
            "import os; os.system('sleep 3 & echo quick')", sandbox=False, session_id="s-repl-hang"
        )
        elapsed = time.monotonic() - started

        assert "quick" in result
        assert elapsed < 2.5, f"the call must not wait for the grandchild (took {elapsed:.1f}s)"

    def test_the_timeout_path_reaps_instead_of_draining(self, monkeypatch):
        """A timeout must use the bounded reap, never an unbounded communicate()."""
        import subprocess as sp

        import agent.tools.python_repl as repl_mod

        calls: list[str] = []

        class _HangingProc:
            returncode = -9

            def __init__(self, *args, **kwargs):
                pass

            def wait(self, timeout=None):
                calls.append("wait")
                raise sp.TimeoutExpired("cmd", timeout or 0)

            def communicate(self, timeout=None):  # pragma: no cover
                calls.append("communicate")
                raise AssertionError("the timeout path must not drain a pipe")

            def kill(self):
                calls.append("kill")

            def terminate(self):
                calls.append("terminate")

            def poll(self):
                return None

        monkeypatch.setattr(repl_mod.subprocess, "Popen", lambda *a, **kw: _HangingProc())
        monkeypatch.setattr(
            repl_mod, "kill_process_tree", lambda proc, **kw: calls.append("kill_tree")
        )

        result, _ = repl_mod._run_with_timeout("1", timeout=1, sandbox=False)

        assert "timed out" in result
        assert "kill_tree" in calls and "communicate" not in calls
