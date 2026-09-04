"""Task 8 tests: HITL sandbox-bypass approval + scope-guard wiring + YOLO public API.

Written FIRST (TDD RED) against `.omo/plans/sandbox-hardening.md` Task 8 (line
732). GREEN comes from:

- ``approval.py``: public ``is_yolo_mode(config)`` (verbatim migration of the
  ``_is_yolo_active`` logic) + ``_is_yolo_active`` kept as a backward-compatible
  alias.
- ``core.py`` ``after_model``: the terminal branch AND a NEW ``python_repl``
  branch insert the sandbox-bypass approval with the exact check order
  (Metis ruling, plan line 739):
      1. scope/policy denial is NOT repeated here — the TOOL layer already
         raises ``ToolException`` (``_deny_sandbox_bypass`` in
         terminal.py/python_repl.py) for non-main ``caller_scope`` +
         ``sandbox=False`` and for ``SANDBOX_POLICY=required`` +
         ``sandbox=False`` (defensive comment only).
      2. ``is_yolo_mode(self.config)`` True → pass through (YOLO needs no
         approval).
      3. False and ``sandbox=False`` → interrupt approval ("沙箱绕过");
         approved → tool call proceeds with ``sandbox=False``; denied →
         rejection ToolMessage ("User denied: ... " + BLOCKED_MESSAGE).
  ``sandbox=True`` calls NEVER enter the bypass approval (straight pass-through,
  no interrupt). The sandbox value is read from the tool_call args exactly like
  the core.py commands extraction (``args dict get with default True``).
- ``heartbeat.py`` / cron ``base.py``: every built tool instance stamped
  ``metadata["caller_scope"] = "background"`` (guard for missing metadata dict).
  Background graphs get NO HumanInTheLoop middleware (tool-level guards cover
  them) — modeled below with middleware-less graphs.

RESUME CONTRACT (documented decision): the bypass approval REUSES the
``interrupt(HITLRequest(...))`` template (core.py:322-329) — the exact
mechanism Task 5 locked in the characterization suite. Resume shape:
``Command(resume={"decisions": [{"type": "approve"}]})`` or
``{"type": "reject", "message": ...}``; a denial injects a
``"User denied: <msg>. <BLOCKED_MESSAGE>"`` error ToolMessage and there is NO
second interrupt.

Env-scrub guarantee on the APPROVE path (user's original design —
"沙箱绕过后仍有防护： env scrub + 黑名单 + cwd 钳制"):
``subprocess.Popen`` is patched GLOBALLY (``monkeypatch.setattr(subprocess,
"Popen", ...)``), the resumed graph runs the REAL tool, and the recorded
``env=`` kwarg must contain no ``SHERRY_SECRET_NAMES`` member (e.g. no
``*_API_KEY``).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any, ClassVar

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import ToolException, tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agent.middlewares.humanInTheLoop import (
    BLOCKED_MESSAGE,
    ApprovalMode,
    HITLConfig,
    HumanInTheLoop,
)
from agent.tools.pub_base.env_scrub import SHERRY_SECRET_NAMES
from agent.tools.python_repl import TimedPythonREPLTool, build_python_repl_tool
from agent.tools.terminal import SafeShellTool, build_terminal_tool
from config import ROOT_DIR

pytestmark = [
    # The upstream ShellInput validator warns on every validation; keep output clean.
    pytest.mark.filterwarnings("ignore:The shell tool has no safeguards"),
]

_SENTINEL_TERMINAL = "__TERMINAL_EXECUTED__"
_SENTINEL_REPL = "__REPL_EXECUTED__"


# ─────────────────────────────────────────────────────────────────────────────
# Scripted model + real-graph harness (mirrors tests/module characterization
# and tests/full/test_hitl_real_graph.py)
# ─────────────────────────────────────────────────────────────────────────────
class _ScriptedModel(BaseChatModel):
    """Emits one scripted tool call, then idles."""

    calls: ClassVar[int] = 0
    scripted_calls: ClassVar[list[dict[str, Any]]] = []

    @property
    def _llm_type(self) -> str:
        return "stub-hitl-sandbox-bypass"

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


def _build_graph(
    scripted_calls: list[dict[str, Any]], tools=(), hitl_config: HITLConfig | None = None
):
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


def _single_sandbox_false_call(tool_name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    payload = dict(args)
    payload["sandbox"] = False
    return {"name": tool_name, "args": payload, "id": call_id, "type": "tool_call"}


@tool("terminal")
def _fake_terminal(command: str = "", commands: list[str] | None = None) -> str:
    """Stub terminal tool: never runs anything, returns a sentinel."""
    return _SENTINEL_TERMINAL


@tool("python_repl")
def _fake_python_repl(query: str = "") -> str:
    """Stub python_repl tool: never runs anything, returns a sentinel."""
    return _SENTINEL_REPL


# ─────────────────────────────────────────────────────────────────────────────
# Spawn-point mocks — GLOBAL subprocess patching (notepad rule: patch the
# global subprocess.Popen, never terminal.subprocess / module attrs).
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


def _seed_fake_secrets(monkeypatch) -> None:
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


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 2a — YOLO public API + pass-through (-k "yolo")
# ─────────────────────────────────────────────────────────────────────────────
class TestYoloPublicApi:
    """is_yolo_mode() public function + _is_yolo_active backward-compat alias."""

    def test_is_yolo_mode_public_api_and_alias(self, monkeypatch):
        from agent.middlewares.humanInTheLoop.approval import _is_yolo_active, is_yolo_mode

        # Public function + backward-compatible alias coexist.
        assert is_yolo_mode is not None
        assert _is_yolo_active is not None

        # Condition 1: config.yolo_mode
        cfg = HITLConfig(yolo_mode=True)
        assert is_yolo_mode(cfg) is True
        # Condition 2: mode == OFF
        cfg = HITLConfig(mode=ApprovalMode.OFF)
        assert is_yolo_mode(cfg) is True
        # Condition 3: env SHERRY_YOLO_MODE in ("1", "true", "yes")
        for value in ("1", "true", "yes"):
            monkeypatch.setenv("SHERRY_YOLO_MODE", value)
            assert is_yolo_mode(HITLConfig()) is True, f"env {value!r}"
        for value in ("0", "false", "", "no"):
            monkeypatch.setenv("SHERRY_YOLO_MODE", value)
            assert is_yolo_mode(HITLConfig()) is False, f"env {value!r}"
        monkeypatch.delenv("SHERRY_YOLO_MODE", raising=False)
        assert is_yolo_mode(HITLConfig()) is False

        # Alias equivalence across all conditions.
        monkeypatch.setenv("SHERRY_YOLO_MODE", "1")
        cfgs = [HITLConfig(), HITLConfig(yolo_mode=True), HITLConfig(mode=ApprovalMode.OFF)]
        for cfg in cfgs:
            assert _is_yolo_active(cfg) == is_yolo_mode(cfg), "alias must delegate"

    def test_yolo_on_terminal_sandbox_false_no_interrupt_direct_execution(self):
        # YOLO on (config) → sandbox=False terminal call passes through with NO
        # interrupt, NO deny message; the tool executes.
        graph, _hitl = _build_graph(
            scripted_calls=[
                _single_sandbox_false_call("terminal", {"command": "echo ok"}, "call_bp_yolo_t")
            ],
            tools=[_fake_terminal],
            hitl_config=HITLConfig(yolo_mode=True),
        )

        out, config = _invoke(graph, "bp-yolo-t-1", "bp-yolo-t-1")

        assert _pending_tasks(graph, config) == [], "YOLO must not interrupt"
        assert any(m.content == _SENTINEL_TERMINAL for m in _tool_messages(out))
        assert not [m for m in _tool_messages(out) if getattr(m, "status", None) == "error"], (
            "YOLO pass must not produce a deny/error ToolMessage"
        )

    def test_yolo_on_python_repl_sandbox_false_no_interrupt(self):
        graph, _hitl = _build_graph(
            scripted_calls=[
                _single_sandbox_false_call("python_repl", {"query": "print(1)"}, "call_bp_yolo_r")
            ],
            tools=[_fake_python_repl],
            hitl_config=HITLConfig(yolo_mode=True),
        )

        out, config = _invoke(graph, "bp-yolo-r-1", "bp-yolo-r-1")

        assert _pending_tasks(graph, config) == [], "YOLO must not interrupt python_repl"
        assert any(m.content == _SENTINEL_REPL for m in _tool_messages(out))
        assert not [m for m in _tool_messages(out) if getattr(m, "status", None) == "error"]

    def test_yolo_env_var_sherry_yolo_mode_passes_sandbox_false(self, monkeypatch):
        # The env-var YOLO condition must route through the same public
        # is_yolo_mode() check in the bypass-approval wiring.
        monkeypatch.setenv("SHERRY_YOLO_MODE", "1")
        graph, _hitl = _build_graph(
            scripted_calls=[
                _single_sandbox_false_call("terminal", {"command": "echo ok"}, "call_bp_yolo_env")
            ],
            tools=[_fake_terminal],
        )

        out, config = _invoke(graph, "bp-yolo-env-1", "bp-yolo-env-1")

        assert _pending_tasks(graph, config) == [], "env-var YOLO must not interrupt"
        assert any(m.content == _SENTINEL_TERMINAL for m in _tool_messages(out))

    def test_yolo_off_sandbox_true_never_interrupts_terminal_and_repl(self):
        # sandbox=True terminal/python_repl calls must NOT enter the HITL
        # bypass approval at all — straight pass-through, no interrupt.
        graph, _hitl = _build_graph(
            scripted_calls=[
                {
                    "name": "terminal",
                    "args": {"command": "echo ok", "sandbox": True},
                    "id": "call_bp_sbt",
                    "type": "tool_call",
                },
                {
                    "name": "python_repl",
                    "args": {"query": "print(1)", "sandbox": True},
                    "id": "call_bp_sbr",
                    "type": "tool_call",
                },
            ],
            tools=[_fake_terminal, _fake_python_repl],
        )

        out, config = _invoke(graph, "bp-sbtrue-1", "bp-sbtrue-1")

        assert _pending_tasks(graph, config) == [], "sandbox=True must never interrupt"
        assert any(m.content == _SENTINEL_TERMINAL for m in _tool_messages(out))
        assert any(m.content == _SENTINEL_REPL for m in _tool_messages(out))
        assert not [m for m in _tool_messages(out) if getattr(m, "status", None) == "error"]


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 1 — main-session bypass approval flow (-k "main")
# ─────────────────────────────────────────────────────────────────────────────
class TestMainSessionBypassApproval:
    """YOLO off + sandbox=False → interrupt('沙箱绕过'); approve/deny contracts."""

    def test_main_terminal_sandbox_false_persists_interrupt_with_shabox_text(self):
        graph, _hitl = _build_graph(
            scripted_calls=[
                _single_sandbox_false_call("terminal", {"command": "echo ok"}, "call_bp_main_t")
            ],
            tools=[_fake_terminal],
        )

        out, config = _invoke(graph, "bp-main-t-1", "bp-main-t-1")

        # First invoke RETURNS normally; the interrupt is pending, not swallowed.
        swallowed = [
            m
            for m in _tool_messages(out)
            if getattr(m, "name", None) == "terminal"
            and "Approval interrupt failed" in (m.content or "")
        ]
        assert not swallowed, "GraphInterrupt must not be swallowed into a deny ToolMessage"

        tasks = _pending_tasks(graph, config)
        assert len(tasks) == 1, f"expected 1 pending interrupt task, got {len(tasks)}"
        assert tasks[0].name == "HumanInTheLoop.after_model"
        payload = tasks[0].interrupts[0].value
        assert payload.get("action_requests"), "payload must include action_requests"
        first_action = payload["action_requests"][0]
        assert first_action["name"] == "terminal"
        assert first_action["args"]["sandbox"] is False
        assert "沙箱绕过" in str(first_action.get("description", "")), (
            "approval text must mention 沙箱绕过"
        )
        assert payload["review_configs"][0]["allowed_decisions"] == ["approve", "reject"]

    def test_main_terminal_sandbox_false_resume_approve_executes_with_scrubbed_env(
        self, monkeypatch
    ):
        # REAL SafeShellTool in the graph; GLOBAL Popen mock; the approved
        # resume must hand the tool sandbox=False AND still spawn with a
        # scrubbed env + clamped cwd (user's original design guarantee).
        _seed_fake_secrets(monkeypatch)
        popen_calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            subprocess, "Popen", lambda *a, **kw: _FakeShellProc(popen_calls, *a, **kw)
        )

        captured_run: list[dict[str, Any]] = []
        original_run = SafeShellTool._run

        def spy_run(self, commands, run_manager=None, sandbox=True, **kwargs):
            captured_run.append({"commands": commands, "sandbox": sandbox})
            return original_run(self, commands, run_manager=run_manager, sandbox=sandbox, **kwargs)

        monkeypatch.setattr(SafeShellTool, "_run", spy_run)

        graph, _hitl = _build_graph(
            scripted_calls=[
                _single_sandbox_false_call("terminal", {"commands": ["echo ok"]}, "call_bp_appr_t")
            ],
            tools=[build_terminal_tool()],
        )

        _out, config = _invoke(graph, "bp-appr-t-1", "bp-appr-t-1")
        assert len(_pending_tasks(graph, config)) == 1

        graph.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)

        assert _pending_tasks(graph, config) == []
        assert captured_run, "approved call must reach the tool layer"
        assert captured_run[0]["sandbox"] is False, "tool must receive sandbox=False"
        assert captured_run[0]["commands"] == ["echo ok"]

        assert popen_calls, "approved sandbox=False call must execute via Popen"
        kwargs = popen_calls[0]["kwargs"]
        _assert_scrubbed_env(kwargs.get("env"))
        assert kwargs.get("cwd") == str(ROOT_DIR), "cwd clamp must stay unconditional"

    def test_main_terminal_sandbox_false_resume_deny_rejection_no_second_interrupt(
        self, monkeypatch
    ):
        popen_calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            subprocess, "Popen", lambda *a, **kw: _FakeShellProc(popen_calls, *a, **kw)
        )

        graph, _hitl = _build_graph(
            scripted_calls=[
                _single_sandbox_false_call("terminal", {"commands": ["echo ok"]}, "call_bp_deny_t")
            ],
            tools=[build_terminal_tool()],
        )

        _out, config = _invoke(graph, "bp-deny-t-1", "bp-deny-t-1")
        assert len(_pending_tasks(graph, config)) == 1

        graph.invoke(
            Command(resume={"decisions": [{"type": "reject", "message": "No way"}]}),
            config,
        )

        # NO second interrupt: the graph completed.
        assert _pending_tasks(graph, config) == [], "deny must not leave a pending interrupt"
        denied = [
            m
            for m in graph.get_state(config).values.get("messages", [])
            if isinstance(m, ToolMessage)
            and getattr(m, "name", None) == "terminal"
            and getattr(m, "status", None) == "error"
        ]
        assert denied, "denied sandbox=False call must produce a rejection ToolMessage"
        content = denied[0].content
        assert "User denied" in content
        assert "No way" in content
        assert BLOCKED_MESSAGE in content
        assert popen_calls == [], "denied call must never spawn a subprocess"

    def test_main_python_repl_sandbox_false_persists_interrupt(self):
        graph, _hitl = _build_graph(
            scripted_calls=[
                _single_sandbox_false_call("python_repl", {"query": "print(1)"}, "call_bp_main_r")
            ],
            tools=[_fake_python_repl],
        )

        out, config = _invoke(graph, "bp-main-r-1", "bp-main-r-1")

        swallowed = [
            m
            for m in _tool_messages(out)
            if getattr(m, "name", None) == "python_repl"
            and "Approval interrupt failed" in (m.content or "")
        ]
        assert not swallowed, "GraphInterrupt must not be swallowed into a deny ToolMessage"

        tasks = _pending_tasks(graph, config)
        assert len(tasks) == 1, f"expected 1 pending interrupt task, got {len(tasks)}"
        assert tasks[0].name == "HumanInTheLoop.after_model"
        payload = tasks[0].interrupts[0].value
        first_action = payload["action_requests"][0]
        assert first_action["name"] == "python_repl"
        assert first_action["args"]["sandbox"] is False
        assert "沙箱绕过" in str(first_action.get("description", ""))
        assert payload["review_configs"][0]["allowed_decisions"] == ["approve", "reject"]

    def test_main_python_repl_sandbox_false_resume_approve_executes_with_scrubbed_env(
        self, monkeypatch
    ):
        _seed_fake_secrets(monkeypatch)
        popen_calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            subprocess, "Popen", lambda *a, **kw: _FakeReplProc(popen_calls, *a, **kw)
        )

        captured_run: list[dict[str, Any]] = []
        original_run = TimedPythonREPLTool._run

        def spy_run(self, query, run_manager=None, sandbox=True, **kwargs):
            captured_run.append({"query": query, "sandbox": sandbox})
            return original_run(self, query, run_manager=run_manager, sandbox=sandbox, **kwargs)

        monkeypatch.setattr(TimedPythonREPLTool, "_run", spy_run)

        graph, _hitl = _build_graph(
            scripted_calls=[
                _single_sandbox_false_call("python_repl", {"query": "print(1)"}, "call_bp_appr_r")
            ],
            tools=[build_python_repl_tool()],
        )

        _out, config = _invoke(graph, "bp-appr-r-1", "bp-appr-r-1")
        assert len(_pending_tasks(graph, config)) == 1

        graph.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)

        assert _pending_tasks(graph, config) == []
        assert captured_run, "approved call must reach the tool layer"
        assert captured_run[0]["sandbox"] is False, "tool must receive sandbox=False"

        assert popen_calls, "approved sandbox=False python_repl call must execute via Popen"
        kwargs = popen_calls[0]["kwargs"]
        _assert_scrubbed_env(kwargs.get("env"))
        assert kwargs.get("cwd") == str(ROOT_DIR), "cwd clamp must stay unconditional"

    def test_main_python_repl_sandbox_false_resume_deny_rejection_no_second_interrupt(
        self, monkeypatch
    ):
        popen_calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            subprocess, "Popen", lambda *a, **kw: _FakeReplProc(popen_calls, *a, **kw)
        )

        graph, _hitl = _build_graph(
            scripted_calls=[
                _single_sandbox_false_call("python_repl", {"query": "print(1)"}, "call_bp_deny_r")
            ],
            tools=[build_python_repl_tool()],
        )

        _out, config = _invoke(graph, "bp-deny-r-1", "bp-deny-r-1")
        assert len(_pending_tasks(graph, config)) == 1

        graph.invoke(
            Command(resume={"decisions": [{"type": "reject", "message": "Not now"}]}),
            config,
        )

        assert _pending_tasks(graph, config) == [], "deny must not leave a pending interrupt"
        denied = [
            m
            for m in graph.get_state(config).values.get("messages", [])
            if isinstance(m, ToolMessage)
            and getattr(m, "name", None) == "python_repl"
            and getattr(m, "status", None) == "error"
        ]
        assert denied, "denied sandbox=False call must produce a rejection ToolMessage"
        content = denied[0].content
        assert "User denied" in content
        assert "Not now" in content
        assert BLOCKED_MESSAGE in content
        assert popen_calls == [], "denied call must never spawn a subprocess"


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 2b — background scope guards (-k "background")
# ─────────────────────────────────────────────────────────────────────────────
class TestBackgroundScopeGuard:
    """heartbeat/cron stamping + the tool-layer guard background graphs rely on."""

    def test_heartbeat_tools_stamped_background_scope(self):
        # heartbeat.py builds its tools at MODULE level; every instance must be
        # stamped metadata["caller_scope"] = "background" after the build calls.
        import server.service.heartbeat as hb

        assert len(hb.tools) >= 1
        for tool_obj in hb.tools:
            metadata = getattr(tool_obj, "metadata", None)
            assert isinstance(metadata, dict), (
                f"{type(tool_obj).__name__} must carry a metadata dict"
            )
            assert metadata.get("caller_scope") == "background", (
                f"{type(tool_obj).__name__} must be stamped caller_scope=background"
            )

    def test_cron_job_tools_stamped_background_scope(self, monkeypatch):
        # cron base.py builds its tools INSIDE _on_cron_job (function-local);
        # stub the LLM/agent wiring and capture the tools list handed to
        # create_agent — every tool must be stamped "background".
        from skills.builtin.core.cron.scripts import base as cron_base
        from skills.builtin.core.cron.scripts.types import CronJob, CronPayload

        recorded: dict[str, Any] = {}

        class _FakeAgent:
            async def ainvoke(self, *args, **kwargs):
                # base.py calls agent.ainvoke(input={...}) with `input` as a KWARG.
                return {"messages": [type("M", (), {"content": "cron-ok"})()]}

        def fake_create_agent(**kwargs):
            recorded["tools"] = kwargs.get("tools")
            return _FakeAgent()

        class _FakeBus:
            async def publish_inbound(self, msg):
                recorded["published"] = msg

        class _FakeChannelManager:
            def get_bus(self):
                return _FakeBus()

        monkeypatch.setattr(cron_base, "build_main_llm", lambda: None)
        monkeypatch.setattr(cron_base, "build_system_prompt", lambda: "sp")
        monkeypatch.setattr(cron_base, "create_agent", fake_create_agent)
        monkeypatch.setattr(cron_base, "channel_manager", _FakeChannelManager())

        job = CronJob(
            id="t8cron01",
            name="task8-stamp-probe",
            payload=CronPayload(message="probe", channel=None, to=None),
        )
        asyncio.run(cron_base._on_cron_job(job))

        tools = recorded.get("tools")
        assert tools, "cron job must build its tool list"
        for tool_obj in tools:
            metadata = getattr(tool_obj, "metadata", None)
            assert isinstance(metadata, dict), (
                f"{type(tool_obj).__name__} must carry a metadata dict"
            )
            assert metadata.get("caller_scope") == "background", (
                f"{type(tool_obj).__name__} must be stamped caller_scope=background"
            )

    def test_background_stamped_python_repl_sandbox_false_tool_layer_exception(self):
        # A background graph (NO HITL middleware — heartbeat/cron never get one)
        # with a background-stamped python_repl: sandbox=False → the TOOL layer
        # raises ToolException (guard text mentions 沙箱绕过仅限主会话) — no
        # interrupt anywhere.
        repl = build_python_repl_tool()
        repl.metadata = {"idempotent": False, "caller_scope": "background"}
        _ScriptedModel.calls = 0
        _ScriptedModel.scripted_calls = [
            _single_sandbox_false_call("python_repl", {"query": "print(1)"}, "call_bp_bg_r")
        ]
        graph = create_agent(model=_ScriptedModel(), tools=[repl])

        out = graph.invoke({"messages": [HumanMessage(content="run it")]})

        errors = [
            m
            for m in out["messages"]
            if isinstance(m, ToolMessage) and getattr(m, "status", None) == "error"
        ]
        assert errors, "background python_repl sandbox=False must be denied by the tool layer"
        assert "沙箱绕过仅限主会话" in str(errors[0].content)

    def test_background_stamped_terminal_sandbox_false_tool_layer_exception(self, monkeypatch):
        # Spawn-point stubbed (belt-and-braces): whatever the guard does, no
        # real subprocess can be spawned by this test.
        monkeypatch.setattr(
            SafeShellTool, "_run_with_encoding", lambda self, cmds, encoding, **kw: "__EXEC__"
        )
        term = build_terminal_tool()
        term.metadata = {"idempotent": False, "caller_scope": "background"}
        _ScriptedModel.calls = 0
        _ScriptedModel.scripted_calls = [
            _single_sandbox_false_call("terminal", {"commands": ["echo ok"]}, "call_bp_bg_t")
        ]
        graph = create_agent(model=_ScriptedModel(), tools=[term])

        out = graph.invoke({"messages": [HumanMessage(content="run it")]})

        errors = [
            m
            for m in out["messages"]
            if isinstance(m, ToolMessage) and getattr(m, "status", None) == "error"
        ]
        assert errors, "background terminal sandbox=False must be denied by the tool layer"
        assert "沙箱绕过仅限主会话" in str(errors[0].content)
        assert "__EXEC__" not in str(errors[0].content)


# ─────────────────────────────────────────────────────────────────────────────
# Subagent scope stamp — lock the EXISTING spawn-path behavior (no code change
# allowed there; this test only pins it)
# ─────────────────────────────────────────────────────────────────────────────
class TestSubagentScopeStamp:
    """inherited_tool_policy.py:102-111 — spawn path stamps caller_scope='subagent'."""

    def test_subagent_spawn_path_stamps_caller_scope_subagent(self):
        from agent.tools.subagent.spawn.inherited_tool_policy import apply_tool_policy

        term = build_terminal_tool()
        repl = build_python_repl_tool()
        kept = apply_tool_policy([term, repl], tool_allow=None, tool_deny=[])

        assert {t.name for t in kept} == {"terminal", "python_repl"}
        for tool_obj in kept:
            assert tool_obj.metadata.get("caller_scope") == "subagent", (
                f"{tool_obj.name} must be stamped caller_scope=subagent"
            )
            assert tool_obj.metadata.get("idempotent") is False, "existing metadata keys preserved"

        # A tool WITHOUT a metadata dict gets one created safely (:109 path).
        @tool("plain_stub")
        def _plain_stub(x: str = "") -> str:
            """Metadata-less stub tool."""
            return "ok"

        assert getattr(_plain_stub, "metadata", None) is None
        kept2 = apply_tool_policy([_plain_stub], tool_allow=None, tool_deny=[])
        assert kept2 and kept2[0].metadata.get("caller_scope") == "subagent"

    def test_tool_exception_guard_message_shapes(self):
        # Lock the tool-layer guard messages the HITL layer deliberately does
        # NOT duplicate (defensive-comment contract in core.py).
        term = build_terminal_tool()
        term.metadata = {"idempotent": False, "caller_scope": "subagent"}
        with pytest.raises(ToolException) as exc_info:
            term._run(["echo ok"], sandbox=False)
        assert "沙箱绕过仅限主会话" in str(exc_info.value)

        repl = build_python_repl_tool()
        repl.metadata = {"idempotent": False, "caller_scope": "subagent"}
        with pytest.raises(ToolException) as exc_info:
            repl._run("print(1)", sandbox=False)
        assert "沙箱绕过仅限主会话" in str(exc_info.value)
