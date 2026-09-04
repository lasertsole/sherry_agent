"""Task 9: sandbox precedence matrix + end-to-end behavior lock.

Cell-by-cell against the AUTHORITATIVE table in the module docstring of
``agent/tools/pub_base/sandbox.py`` (Task 2):

    ========== ============================== ==============================
    policy     sandbox=True                   sandbox=False
    ========== ============================== ==============================
    required   sandboxed via backend;         DENIED outright (tool layer,
               RuntimeError if the backend    Tasks 6/7 -- no approval path)
               is unavailable on this system
    auto       sandboxed via backend; if      HITL approval required
               unavailable: degrade to        (Task 8, humanInTheLoop)
               unsandboxed + one loguru
               warning (warning emitted by
               the tool layer, Tasks 6/7)
    off        NEVER sandboxed, NEVER         NEVER sandboxed, NEVER
               approved                       approved
    ========== ============================== ==============================

Coverage map (cell -> tests):

1. required + sandbox=True + backend available (mock)
   -> ``TestCell1RequiredSandboxTrueBackendAvailable``: both tools exec the
   backend-wrapped argv in list-exec form (no shell), env = wrapped env.
2. required + sandbox=True + backend unavailable
   -> ``TestCell2RequiredSandboxTrueBackendUnavailable``: terminal wraps the
   get_backend ``RuntimeError`` into ``ToolException`` (surfaced verbatim by
   ``handle_tool_error=True``); python_repl surfaces the raw ``RuntimeError``
   (matrix allows "RuntimeError / explicit error"); NOTHING is spawned.
3. required + sandbox=False
   -> ``TestCell3RequiredSandboxFalseToolDenial``: tool-layer ``ToolException``
   from ``_deny_sandbox_bypass`` -- NOT a ``GraphInterrupt`` -- and no spawn.
4. auto + sandbox=False + non-YOLO
   -> ``TestCell4AutoSandboxFalseNonYoloGraphInterrupt``: cross-component
   (real ``HumanInTheLoop`` middleware + real tool in a real ``create_agent``
   graph): first invoke RETURNS normally with a PENDING interrupt
   (``graph.get_state(config).tasks``), never swallowed into a deny
   ToolMessage (Task 8 ``_pending_tasks`` pattern).
5. auto + sandbox=True + backend unavailable (Windows degrade)
   -> ``TestCell5AutoSandboxTrueUnavailableDegradedOneWarning``: degraded
   DIRECT execution + EXACTLY ONE loguru warning. Cross-component spec
   (plan line 829): global ``subprocess.Popen`` mock asserts BOTH the
   scrubbed env dict AND the command string/argv -- one test per tool.
6. off + any
   -> ``TestCell6OffNeverSandboxNeverApproved``: no sandbox wrap, no backend
   consult, no degrade warning, no approval needed: ``sandbox=False``
   executes directly (no approver exists in off mode, consistent with the
   matrix).

Mock discipline (inherited rules):
- ``subprocess.Popen`` is patched GLOBALLY (``monkeypatch.setattr(subprocess,
  "Popen", ...)``), never ``terminal.subprocess`` / module attrs.
- Backends are mocked via ``get_backend`` module-attr stubs; bwrap/seatbelt
  are NEVER probed for real (Windows host).
- ``SANDBOX_POLICY`` is set through the environment so the REAL
  ``read_policy`` (os.getenv on every call) is exercised in every cell.
- loguru warnings are captured with a ``logger.add`` sink (loguru does not
  propagate to stdlib caplog).
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from contextlib import contextmanager
from typing import Any, ClassVar

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import ToolException
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphInterrupt
from loguru import logger

import agent.tools.python_repl as python_repl_mod
import agent.tools.terminal as terminal_mod
from agent.middlewares.humanInTheLoop import HITLConfig, HumanInTheLoop
from agent.tools.pub_base.env_scrub import SHERRY_SECRET_NAMES
from agent.tools.python_repl import build_python_repl_tool
from agent.tools.terminal import build_terminal_tool
from config import ROOT_DIR

pytestmark = [
    # The upstream ShellInput validator warns on every validation; keep output clean.
    pytest.mark.filterwarnings("ignore:The shell tool has no safeguards"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Backend stand-ins
# ─────────────────────────────────────────────────────────────────────────────
class _RecordingBackend:
    """SandboxBackend stand-in: probe()=True, wrap() records + marks the env."""

    def __init__(self, wrapper: str = "fakewrap"):
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self._wrapper = wrapper

    def probe(self) -> bool:
        return True

    def wrap(
        self, cmd: list[str], env: dict[str, str]
    ) -> tuple[list[str], dict[str, str]]:
        self.calls.append((list(cmd), dict(env)))
        return [self._wrapper, *cmd], {**env, "FAKE_SANDBOX": "1"}


def _forbidden_get_backend(policy: Any) -> Any:
    raise AssertionError(f"get_backend must not be consulted in this cell (policy={policy!r})")


def _unavailable_get_backend(policy: Any) -> Any:
    """Contract-faithful stand-in for get_backend on an unavailable backend."""
    raise RuntimeError(f"Required sandbox unavailable on {platform.system()}")


# ─────────────────────────────────────────────────────────────────────────────
# Spawn-point mocks — GLOBAL subprocess.Popen patching (notepad rule)
# ─────────────────────────────────────────────────────────────────────────────
class _FakeShellProc:
    """Stands in for terminal's subprocess.Popen[bytes]."""

    returncode = 0

    def __init__(self, recorder: list[dict[str, Any]], *args: Any, **kwargs: Any):
        recorder.append({"args": args, "kwargs": kwargs})

    def communicate(self, timeout=None):
        return (b"ok-out", b"")

    def kill(self):
        pass


class _FakeReplProc:
    """Stands in for python_repl's subprocess.Popen[str] (text=True)."""

    returncode = 0

    def __init__(self, recorder: list[dict[str, Any]], *args: Any, **kwargs: Any):
        recorder.append({"args": args, "kwargs": kwargs})

    def communicate(self, timeout=None):
        payload = json.dumps({"out": "1\n", "err": "", "exc": None, "tb": None})
        return (payload, "")

    def kill(self):
        pass


def _install_fake_popen(monkeypatch: pytest.MonkeyPatch, proc_cls: type) -> list[dict[str, Any]]:
    recorder: list[dict[str, Any]] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: proc_cls(recorder, *a, **kw))
    return recorder


def _install_never_spawn_popen(monkeypatch: pytest.MonkeyPatch, label: str) -> list[dict[str, Any]]:
    """Popen stub that fails LOUDLY if anything tries to spawn a process."""
    recorder: list[dict[str, Any]] = []

    def _boom(*args: Any, **kwargs: Any):
        recorder.append({"args": args, "kwargs": kwargs})
        raise AssertionError(f"{label}: subprocess must never spawn (got {args!r} kwargs={kwargs!r})")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    return recorder


def _seed_fake_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed os.environ with fake secrets the scrub layer must drop."""
    monkeypatch.setenv("MAIN_LLM_API_KEY", "sk-fake-secret-1")  # deny-by-name member
    monkeypatch.setenv("MY_CUSTOM_TOKEN", "tok-fake-2")  # substring-block (TOKEN)


def _assert_scrubbed_env(env: Any) -> None:
    assert isinstance(env, dict), f"Popen env kwarg must be a dict, got {type(env)!r}"
    assert "MAIN_LLM_API_KEY" not in env, "seeded secret MAIN_LLM_API_KEY reached the child"
    assert "MY_CUSTOM_TOKEN" not in env, "seeded substring-block var reached the child"
    for name in SHERRY_SECRET_NAMES:
        assert name not in env, f"secret var {name} reached the child process"
    assert "PATH" in env, "sanity: scrubbed env must keep PATH"


@contextmanager
def _loguru_warnings():
    """Capture loguru WARNING+ records (loguru does not reach stdlib caplog)."""
    records: list[str] = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        yield records
    finally:
        logger.remove(sink_id)


# ─────────────────────────────────────────────────────────────────────────────
# Real-graph harness (mirrors tests/module/test_hitl_sandbox_bypass.py)
# ─────────────────────────────────────────────────────────────────────────────
class _ScriptedModel(BaseChatModel):
    """Emits one scripted tool call, then idles."""

    calls: ClassVar[int] = 0
    scripted_calls: ClassVar[list[dict[str, Any]]] = []

    @property
    def _llm_type(self) -> str:
        return "stub-sandbox-matrix"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        type(self).calls += 1
        if type(self).calls == 1 and type(self).scripted_calls:
            msg = AIMessage(content="", tool_calls=list(type(self).scripted_calls))
        else:
            msg = AIMessage(content="Done (no further tool calls).")
        return ChatResult(generations=[ChatGeneration(message=msg)])


class _HarnessState(AgentState):
    session_id: str


def _build_graph(scripted_calls: list[dict[str, Any]], tools=(), hitl_config: HITLConfig | None = None):
    """Real create_agent + real HumanInTheLoop; the stub model drives it."""
    _ScriptedModel.calls = 0
    _ScriptedModel.scripted_calls = list(scripted_calls)

    hitl = HumanInTheLoop(hitl_config or HITLConfig())
    graph = create_agent(
        model=_ScriptedModel(),
        state_schema=_HarnessState,
        checkpointer=MemorySaver(),
        tools=list(tools),
        middleware=[hitl],
    )
    return graph, hitl


def _invoke(graph, thread_id: str, session_id: str):
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    out = graph.invoke(
        {"messages": [HumanMessage(content="please run it")], "session_id": session_id},
        config,
    )
    return out, config


def _tool_messages(out) -> list[ToolMessage]:
    return [m for m in out["messages"] if isinstance(m, ToolMessage)]


def _pending_tasks(graph, config):
    return list(graph.get_state(config).tasks or [])


# ═════════════════════════════════════════════════════════════════════════════
# Cell 1 — required + sandbox=True + backend available (mock) → sandboxed exec
# ═════════════════════════════════════════════════════════════════════════════
class TestCell1RequiredSandboxTrueBackendAvailable:
    """backend.wrap argv is what reaches Popen (list-exec, no shell)."""

    def test_terminal_exec_via_backend_wrap(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("SANDBOX_POLICY", "required")  # real read_policy path
        backend = _RecordingBackend()
        monkeypatch.setattr(terminal_mod, "get_backend", lambda policy: backend)
        _seed_fake_secrets(monkeypatch)
        calls = _install_fake_popen(monkeypatch, _FakeShellProc)

        term = build_terminal_tool()
        out = term._run(["echo ok"], sandbox=True)

        assert out == "ok-out", "sandboxed execution must return the tool output"
        assert len(backend.calls) == 1, "backend.wrap must be consulted exactly once"
        wrapped_cmd, wrapped_env = backend.calls[0]
        assert wrapped_cmd == ["/bin/sh", "-c", "echo ok"], (
            "terminal hands the backend the POSIX-equivalent [sh, -c, cmd] argv"
        )
        # calls record the env INPUT to wrap() (pre-marker): it must be the
        # scrubbed env; the FAKE_SANDBOX marker belongs to the RETURNED env,
        # asserted at the Popen kwargs below.
        _assert_scrubbed_env(wrapped_env)

        assert len(calls) == 1, "exactly one spawn on the sandboxed path"
        argv = calls[0]["args"][0]
        assert isinstance(argv, list), "sandboxed path execs a LIST (no shell)"
        assert argv == ["fakewrap", "/bin/sh", "-c", "echo ok"], (
            "the backend-wrapped argv must be what reaches Popen"
        )
        kwargs = calls[0]["kwargs"]
        assert "shell" not in kwargs, "list-exec form must not pass shell=True"
        assert kwargs["env"].get("FAKE_SANDBOX") == "1", "wrapped env reaches the child"
        _assert_scrubbed_env(kwargs["env"])
        assert kwargs["cwd"] == str(ROOT_DIR), "cwd clamp stays unconditional"

    def test_python_repl_exec_via_backend_wrap(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("SANDBOX_POLICY", "required")
        backend = _RecordingBackend()
        monkeypatch.setattr(python_repl_mod, "get_backend", lambda policy: backend)
        _seed_fake_secrets(monkeypatch)
        calls = _install_fake_popen(monkeypatch, _FakeReplProc)

        repl = build_python_repl_tool()
        out = repl._run("print(1)", sandbox=True)

        assert out == "1\n", "sandboxed execution must return the tool output"
        assert len(backend.calls) == 1
        wrapped_cmd, wrapped_env = backend.calls[0]
        assert wrapped_cmd[0] == sys.executable and wrapped_cmd[1] == "-c", (
            "repl hands the backend the interpreter argv [exe, -c, script]"
        )
        # calls record the env INPUT to wrap() (pre-marker): it must be the
        # scrubbed env; the FAKE_SANDBOX marker belongs to the RETURNED env,
        # asserted at the Popen kwargs below.
        _assert_scrubbed_env(wrapped_env)

        assert len(calls) == 1
        argv = calls[0]["args"][0]
        assert isinstance(argv, list)
        assert argv == ["fakewrap", *wrapped_cmd], (
            "the backend-wrapped argv must be what reaches Popen"
        )
        kwargs = calls[0]["kwargs"]
        assert kwargs.get("text") is True
        assert kwargs["env"].get("FAKE_SANDBOX") == "1"
        _assert_scrubbed_env(kwargs["env"])
        assert kwargs["cwd"] == str(ROOT_DIR), "cwd clamp stays unconditional"


# ═════════════════════════════════════════════════════════════════════════════
# Cell 2 — required + sandbox=True + backend unavailable → explicit error
# ═════════════════════════════════════════════════════════════════════════════
class TestCell2RequiredSandboxTrueBackendUnavailable:
    def test_terminal_wraps_runtime_error_into_tool_exception_no_exec(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("SANDBOX_POLICY", "required")
        monkeypatch.setattr(terminal_mod, "get_backend", _unavailable_get_backend)
        calls = _install_never_spawn_popen(monkeypatch, "cell2-terminal")

        term = build_terminal_tool()
        with pytest.raises(ToolException) as exc_info:
            term._run(["echo ok"], sandbox=True)

        msg = str(exc_info.value)
        assert "Required sandbox unavailable" in msg, (
            f"get_backend's RuntimeError text must surface, got: {msg!r}"
        )
        assert calls == [], "a REQUIRED policy with no backend must never spawn"

        # handle_tool_error=True surfaces the raw exception message at run()
        # level (langchain_core 1.4.7: content = e.args[0]).
        surfaced = term.run({"commands": ["echo ok"], "sandbox": True})
        assert isinstance(surfaced, str)
        assert "Required sandbox unavailable" in surfaced

    def test_python_repl_surfaces_runtime_error_no_exec(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("SANDBOX_POLICY", "required")
        monkeypatch.setattr(python_repl_mod, "get_backend", _unavailable_get_backend)
        calls = _install_never_spawn_popen(monkeypatch, "cell2-repl")

        repl = build_python_repl_tool()
        with pytest.raises(RuntimeError) as exc_info:
            repl._run("print(1)", sandbox=True)

        msg = str(exc_info.value)
        assert "Required sandbox unavailable" in msg, (
            f"matrix cell requires an explicit error, got: {msg!r}"
        )
        assert calls == [], "a REQUIRED policy with no backend must never spawn"


# ═════════════════════════════════════════════════════════════════════════════
# Cell 3 — required + sandbox=False → tool-layer ToolException, NO interrupt
# ═════════════════════════════════════════════════════════════════════════════
class TestCell3RequiredSandboxFalseToolDenial:
    @pytest.mark.parametrize(
        "kind,run_denied",
        [
            ("terminal", lambda term, repl: term._run(["echo ok"], sandbox=False)),
            ("python_repl", lambda term, repl: repl._run("print(1)", sandbox=False)),
        ],
        ids=["terminal", "python_repl"],
    )
    def test_required_policy_denies_sandbox_false_at_tool_layer(
        self, monkeypatch: pytest.MonkeyPatch, kind: str, run_denied
    ):
        monkeypatch.setenv("SANDBOX_POLICY", "required")
        _install_never_spawn_popen(monkeypatch, f"cell3-{kind}")

        term = build_terminal_tool()
        repl = build_python_repl_tool()

        with pytest.raises(ToolException) as exc_info:
            run_denied(term, repl)

        assert not isinstance(exc_info.value, GraphInterrupt), (
            "policy denial is a tool-layer ToolException, never an interrupt"
        )
        msg = str(exc_info.value)
        assert "required" in msg, f"deny text must name the policy, got: {msg!r}"
        assert ("主会话" in msg) or ("main" in msg), (
            f"deny text must mention the main-session approval path, got: {msg!r}"
        )


# ═════════════════════════════════════════════════════════════════════════════
# Cell 4 — auto + sandbox=False + non-YOLO → GraphInterrupt (HITL + tool)
# ═════════════════════════════════════════════════════════════════════════════
class TestCell4AutoSandboxFalseNonYoloGraphInterrupt:
    @pytest.mark.parametrize(
        "tool_name,tool_args,call_id",
        [
            ("terminal", {"commands": ["echo ok"]}, "call_mx_int_t"),
            ("python_repl", {"query": "print(1)"}, "call_mx_int_r"),
        ],
        ids=["terminal", "python_repl"],
    )
    def test_auto_sandbox_false_persists_pending_interrupt(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tool_name: str,
        tool_args: dict[str, Any],
        call_id: str,
    ):
        monkeypatch.setenv("SANDBOX_POLICY", "auto")
        monkeypatch.delenv("SHERRY_YOLO_MODE", raising=False)  # non-YOLO
        _install_never_spawn_popen(monkeypatch, f"cell4-{tool_name}")

        real_tool = build_terminal_tool() if tool_name == "terminal" else build_python_repl_tool()
        scripted = [
            {
                "name": tool_name,
                "args": {**tool_args, "sandbox": False},
                "id": call_id,
                "type": "tool_call",
            }
        ]
        graph, _hitl = _build_graph(scripted, tools=[real_tool])

        # First invoke RETURNS normally; the interrupt stays PENDING.
        out, config = _invoke(graph, f"mx-{call_id}", f"mx-{call_id}")

        error_msgs = [m for m in _tool_messages(out) if getattr(m, "status", None) == "error"]
        assert not error_msgs, (
            "GraphInterrupt must not be swallowed into a deny/error ToolMessage"
        )

        tasks = _pending_tasks(graph, config)
        assert len(tasks) == 1, f"expected 1 pending interrupt task, got {len(tasks)}"
        assert tasks[0].name == "HumanInTheLoop.after_model"
        payload = tasks[0].interrupts[0].value
        assert payload.get("action_requests"), "payload must include action_requests"
        first_action = payload["action_requests"][0]
        assert first_action["name"] == tool_name
        assert first_action["args"]["sandbox"] is False
        assert "沙箱绕过" in str(first_action.get("description", "")), (
            "approval text must mention 沙箱绕过"
        )
        assert payload["review_configs"][0]["allowed_decisions"] == ["approve", "reject"]


# ═════════════════════════════════════════════════════════════════════════════
# Cell 5 — auto + sandbox=True + backend unavailable → degrade + ONE warning
# (cross-component spec, plan line 829: assert BOTH the scrubbed env dict AND
#  the command string/argv passed to Popen — one test per tool)
# ═════════════════════════════════════════════════════════════════════════════
class TestCell5AutoSandboxTrueUnavailableDegradedOneWarning:
    def test_terminal_degrade_direct_exec_env_and_command_string(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("SANDBOX_POLICY", "auto")
        monkeypatch.setattr(terminal_mod, "get_backend", lambda policy: None)  # Windows path
        _seed_fake_secrets(monkeypatch)
        calls = _install_fake_popen(monkeypatch, _FakeShellProc)

        with _loguru_warnings() as records:
            term = build_terminal_tool()
            out = term._run(["echo ok"], sandbox=True)

        assert len(records) == 1, f"exactly ONE degrade warning expected, got {records!r}"
        assert "sandbox" in records[0] and "degrad" in records[0]
        assert out == "ok-out", "degraded call still executes and returns output"

        assert len(calls) == 1
        argv = calls[0]["args"][0]
        assert argv == "echo ok", "degraded terminal path keeps the byte-identical string form"
        kwargs = calls[0]["kwargs"]
        assert kwargs.get("shell") is True, "degraded path is the historical shell=True spawn"
        _assert_scrubbed_env(kwargs.get("env"))  # env scrub must hold on the degrade path
        assert "FAKE_SANDBOX" not in kwargs["env"], "no backend -> no wrapped env markers"
        assert kwargs.get("cwd") == str(ROOT_DIR), "cwd clamp stays unconditional"

    def test_python_repl_degrade_direct_exec_env_and_argv(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("SANDBOX_POLICY", "auto")
        monkeypatch.setattr(python_repl_mod, "get_backend", lambda policy: None)
        _seed_fake_secrets(monkeypatch)
        calls = _install_fake_popen(monkeypatch, _FakeReplProc)

        with _loguru_warnings() as records:
            repl = build_python_repl_tool()
            out = repl._run("print(1)", sandbox=True)

        assert len(records) == 1, f"exactly ONE degrade warning expected, got {records!r}"
        assert "sandbox" in records[0] and "degrad" in records[0]
        assert out == "1\n", "degraded call still executes and returns output"

        assert len(calls) == 1
        argv = calls[0]["args"][0]
        assert isinstance(argv, list), "degraded repl path keeps the list argv"
        assert argv[0] == sys.executable and argv[1] == "-c", (
            "bare interpreter argv [exe, -c, script] without any wrapper"
        )
        kwargs = calls[0]["kwargs"]
        assert kwargs.get("text") is True
        _assert_scrubbed_env(kwargs.get("env"))  # env scrub must hold on the degrade path
        assert kwargs.get("cwd") == str(ROOT_DIR), "cwd clamp stays unconditional"


# ═════════════════════════════════════════════════════════════════════════════
# Cell 6 — off + any → no sandbox, no approval
# ═════════════════════════════════════════════════════════════════════════════
class TestCell6OffNeverSandboxNeverApproved:
    def test_off_terminal_sandbox_false_executes_directly(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("SANDBOX_POLICY", "off")
        monkeypatch.setattr(terminal_mod, "get_backend", _forbidden_get_backend)
        calls = _install_fake_popen(monkeypatch, _FakeShellProc)

        term = build_terminal_tool()
        out = term._run(["echo ok"], sandbox=False)

        # Executes directly: no ToolException, no GraphInterrupt, no approver
        # needed (off mode has no approver, consistent with the matrix).
        assert out == "ok-out"
        assert len(calls) == 1
        assert calls[0]["args"][0] == "echo ok"
        assert calls[0]["kwargs"].get("shell") is True
        _assert_scrubbed_env(calls[0]["kwargs"].get("env"))

    def test_off_python_repl_sandbox_false_executes_directly(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("SANDBOX_POLICY", "off")
        monkeypatch.setattr(python_repl_mod, "get_backend", _forbidden_get_backend)
        calls = _install_fake_popen(monkeypatch, _FakeReplProc)

        repl = build_python_repl_tool()
        out = repl._run("print(1)", sandbox=False)

        assert out == "1\n"
        assert len(calls) == 1
        argv = calls[0]["args"][0]
        assert isinstance(argv, list)
        assert argv[0] == sys.executable and argv[1] == "-c"
        _assert_scrubbed_env(calls[0]["kwargs"].get("env"))

    def test_off_terminal_sandbox_true_no_wrap_no_warning_no_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("SANDBOX_POLICY", "off")
        monkeypatch.setattr(terminal_mod, "get_backend", _forbidden_get_backend)
        calls = _install_fake_popen(monkeypatch, _FakeShellProc)

        with _loguru_warnings() as records:
            term = build_terminal_tool()
            out = term._run(["echo ok"], sandbox=True)

        assert records == [], "policy=off must emit no degrade warning"
        assert out == "ok-out"
        assert len(calls) == 1
        assert calls[0]["args"][0] == "echo ok", "off must NEVER wrap (plain string form)"
        assert calls[0]["kwargs"].get("shell") is True

    def test_off_python_repl_sandbox_true_no_wrap_no_warning_no_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("SANDBOX_POLICY", "off")
        monkeypatch.setattr(python_repl_mod, "get_backend", _forbidden_get_backend)
        calls = _install_fake_popen(monkeypatch, _FakeReplProc)

        with _loguru_warnings() as records:
            repl = build_python_repl_tool()
            out = repl._run("print(1)", sandbox=True)

        assert records == [], "policy=off must emit no degrade warning"
        assert out == "1\n"
        assert len(calls) == 1
        argv = calls[0]["args"][0]
        assert isinstance(argv, list)
        assert argv[0] == sys.executable and argv[1] == "-c", "off must NEVER wrap"
