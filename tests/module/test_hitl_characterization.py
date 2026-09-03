"""Characterization tests: lock CURRENT behavior of HITL flow + terminal/python_repl tools.

Task 5 of `.omo/plans/sandbox-hardening.md` — behavior snapshot taken BEFORE the
sandbox-hardening refactor (Tasks 6/7/8). This suite is GREEN at commit time by
design: it pins today's reality (including known defects) so Tasks 6/7/8 can
refactor against a safety net.

Tag legend (grep-able, one tag per test — MANDATORY):
- ``# PRESERVE``            — behavior must survive Tasks 6/7/8 unchanged.
- ``# WILL-CHANGE(Task N)`` — Task N will update the assertion in this test.

Coverage map (plan Task 5, 6 items):
1. terminal blacklist is ELEMENT-EXACT matching (defect) ....... test_terminal_blacklist_*
2. ShellInput validator normalizes str -> list ................ test_shell_input_validator_*
3. python_repl _REPL_WRAPPER simple execution ................. test_python_repl_*
4. HITL flow (check_command call site, hardline deny, YOLO-off
   interrupt, YOLO-on pass, python_repl not intercepted) ....... test_check_command_*, test_hardline_*,
                                                                test_dangerous_*, test_yolo_*,
                                                                test_interrupt_resume_*, test_python_repl_not_intercepted
5. handle_tool_error=True semantics (ToolException -> error
   ToolMessage channel) ....................................... test_handle_tool_error_*

Graph tests mirror ``tests/full/test_hitl_real_graph.py``: a REAL LangGraph
(``create_agent`` + real ``HumanInTheLoop`` middleware), no LLM — a scripted
``BaseChatModel`` stub emits one terminal/python_repl tool call, then idles.
KNOWN MECHANICS discovered while writing this (see notepad learnings):
- ``interrupt()`` raises ``GraphInterrupt``; ``core.py`` re-raises it (``except
  GraphInterrupt: raise``) so LangGraph persists a pending task on the
  checkpointer. The first ``invoke`` RETURNS normally with the pending task;
  a second ``invoke(Command(resume={"decisions": [...]}))`` resolves it.
- YOLO bypass lives INSIDE ``check_command`` (approval.py layer 3), not in
  ``after_model`` — check_command is still called when YOLO is on.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import ToolException, tool
from langchain_community.tools.shell.tool import ShellInput
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agent.middlewares.humanInTheLoop import (
    BLOCKED_MESSAGE,
    ApprovalMode,
    HITLConfig,
    HumanInTheLoop,
)
from agent.tools.python_repl import TimedPythonREPLTool, build_python_repl_tool
from agent.tools.terminal import SafeShellTool, build_terminal_tool

pytestmark = [
    # The upstream ShellInput validator warns on every validation; keep output clean.
    pytest.mark.filterwarnings("ignore:The shell tool has no safeguards"),
]

# Commands that MUST stay blocked before and after Tasks 6/7/8 (regression floor).
# NOTE: "echo hi && rm -rf /" is blocked only as a RAW STR (substring membership);
# as a list element it is byte-different from every BLACKLIST entry -> bypasses.
_STILL_BLOCKED = ["rm -rf /", "echo hi && rm -rf /"]

# Today's blacklist is ELEMENT-EXACT for list inputs, so these slip past
# SafeShellTool._run today (HITL hardline regex still catches them, but the
# tool layer does not). Task 6's regex blacklist must block them.
_BYPASS_TODAY = [
    ["echo ok && rm -rf /"],      # danger embedded in a larger element
    ["rm -rf /*"],                # not byte-equal to "rm -rf /"
    ["rm  -rf /"],                # double space -> not byte-equal
]

_SENTINEL_TERMINAL = "__TERMINAL_EXECUTED__"
_SENTINEL_REPL = "__REPL_EXECUTED__"


def _blocked_outcome(run, *args, **kwargs) -> str:
    """Normalize today's return-str block vs Task 6's planned ToolException raise.

    Today ``SafeShellTool._run`` RETURNS "Blocked: unsafe command."; Task 6 will
    RAISE ToolException with the same message format (handle_tool_error=True then
    converts it to an error ToolMessage channel). This helper accepts both so the
    PRESERVE assertion stays valid across the mechanism change.
    """
    try:
        return run(*args, **kwargs)
    except ToolException as exc:  # future Task 6 mechanism
        return str(exc)


# ─────────────────────────────────────────────────────────────────────────────
# Coverage 1 — terminal blacklist: current ELEMENT-EXACT matching semantics
# ─────────────────────────────────────────────────────────────────────────────
class TestTerminalBlacklist:
    """agent/tools/terminal.py:12,28-31,49-51 — BLACKLIST + ``bad in commands``."""

    def test_exact_element_and_raw_str_are_blocked(self, monkeypatch):
        # # PRESERVE — raw str (substring membership) and byte-equal list element
        # are blocked today; Task 6's regex blacklist keeps blocking these
        # (message format preserved). _run_with_encoding is stubbed so this test
        # can NEVER spawn a real subprocess, whatever the blacklist does.
        monkeypatch.setattr(
            SafeShellTool, "_run_with_encoding", lambda self, cmds, encoding, **kw: "__EXECUTED__"
        )
        tool = build_terminal_tool()

        for cmd in _STILL_BLOCKED:
            # raw str -> substring membership -> blocked
            out = _blocked_outcome(tool._run, cmd)
            assert "Blocked: unsafe" in out, f"raw str {cmd!r} must be blocked"
            assert out != "__EXECUTED__"

        # single-element list whose element is byte-equal to a BLACKLIST entry -> blocked
        out = _blocked_outcome(tool._run, ["rm -rf /"])
        assert "Blocked: unsafe" in out, "exact element ['rm -rf /'] must be blocked"
        assert out != "__EXECUTED__"

    @pytest.mark.parametrize("commands", _BYPASS_TODAY, ids=lambda c: repr(c))
    def test_element_exact_bypass_reaches_execution(self, commands, monkeypatch):
        # # 特征化：当前缺陷，Task 6 将改为正则
        # # WILL-CHANGE(Task 6)
        # KNOWN DEFECT, locked on purpose: for list inputs ``bad in commands`` is
        # ELEMENT-EXACT equality, so a danger embedded in a larger element (or
        # differing by whitespace/glob) reaches subprocess execution TODAY.
        # Task 6 replaces this with a regex over the joined command string;
        # this assertion will then flip to "blocked".
        captured: list = []

        def fake_run_with_encoding(self, cmds, encoding, **kwargs):
            captured.append(cmds)
            return "__EXECUTED__"

        monkeypatch.setattr(SafeShellTool, "_run_with_encoding", fake_run_with_encoding)
        tool = build_terminal_tool()

        out = tool._run(commands)  # type: ignore[arg-type]

        assert out == "__EXECUTED__", "command should slip past today's blacklist"
        assert captured == [commands], (
            f"element-exact blacklist let {commands!r} through to execution "
            "(known defect, fixed by Task 6 regex)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Coverage 2 — ShellInput validator: str -> list normalization
# ─────────────────────────────────────────────────────────────────────────────
def test_shell_input_validator_normalizes_str_to_list(monkeypatch):
    # # PRESERVE — langchain_community ShellInput model_validator(mode="before")
    # wraps any non-list ``commands`` into a single-element list; an already-list
    # value is kept as-is (no splitting). SafeShellTool inherits this schema.
    assert ShellInput(commands="ls -la").commands == ["ls -la"]
    assert ShellInput(commands=["ls -la"]).commands == ["ls -la"]  # list stays intact

    # End-to-end through the tool invocation path: _run receives a list.
    captured: list = []

    def fake_run(self, commands, **kwargs):
        captured.append(commands)
        return "ok"

    monkeypatch.setattr(SafeShellTool, "_run", fake_run)
    tool = build_terminal_tool()
    tool.invoke({"commands": "ls -la"})
    assert captured == [["ls -la"]], "str input must be normalized to a list before _run"


# ─────────────────────────────────────────────────────────────────────────────
# Coverage 3 — python_repl _REPL_WRAPPER execution semantics
# ─────────────────────────────────────────────────────────────────────────────
class TestPythonReplWrapper:
    """agent/tools/python_repl.py:17-98 — subprocess JSON wrapper protocol."""

    def test_simple_execution_returns_stdout(self):
        # # PRESERVE — print(1+1) -> "2". Task 7 must NOT break the wrapper
        # protocol (plan: "不修改 _REPL_WRAPPER 脚本内容"; QA expects output "2").
        repl = build_python_repl_tool()
        out = repl.run({"query": "print(1+1)"})
        assert "2" in out, f"expected stdout '2' from the wrapper, got {out!r}"

    def test_exception_path_returns_error_string(self):
        # # PRESERVE — the wrapper reports in-code exceptions through its JSON
        # channel as an "Error: ..." string; _run_with_timeout itself does not raise.
        repl = build_python_repl_tool()
        out = repl.run({"query": "1/0"})
        assert isinstance(out, str)
        assert out.startswith("Error:"), f"expected error channel output, got {out!r}"
        assert "ZeroDivisionError" in out


# ─────────────────────────────────────────────────────────────────────────────
# YOLO semantics — the three activation conditions (approval.py:37-49)
# ─────────────────────────────────────────────────────────────────────────────
class TestYoloActivation:
    """_is_yolo_active reads: config.yolo_mode / config.mode==OFF / SHERRY_YOLO_MODE env."""

    def test_check_command_approves_dangerous_command(self, monkeypatch):
        # # PRESERVE — YOLO bypass lives INSIDE check_command (layer 3): when
        # active, even a dangerous command is approved with a "YOLO" reason.
        from agent.middlewares.humanInTheLoop import ApprovalPipeline
        from unittest.mock import MagicMock

        dangerous = "git push --force origin main"

        # Condition 1: HITLConfig(yolo_mode=True) (existing tests construct it this way)
        pipe = ApprovalPipeline(HITLConfig(yolo_mode=True), MagicMock())
        result = pipe.check_command(dangerous, "charz-yolo-1")
        assert result.approved is True
        assert "YOLO" in result.reason

        # Condition 2: HITLConfig(mode=ApprovalMode.OFF)
        pipe = ApprovalPipeline(HITLConfig(mode=ApprovalMode.OFF), MagicMock())
        result = pipe.check_command(dangerous, "charz-yolo-2")
        assert result.approved is True

        # Condition 3: env var SHERRY_YOLO_MODE in {1, true, yes}; other values inert.
        for value, expected in (("1", True), ("true", True), ("yes", True), ("0", False)):
            monkeypatch.setenv("SHERRY_YOLO_MODE", value)
            pipe = ApprovalPipeline(HITLConfig(), MagicMock())
            result = pipe.check_command(dangerous, "charz-yolo-3")
            assert result.approved is expected, f"SHERRY_YOLO_MODE={value!r}"
        monkeypatch.delenv("SHERRY_YOLO_MODE", raising=False)

        # Without any condition: dangerous command escalates (decision=None).
        pipe = ApprovalPipeline(HITLConfig(), MagicMock())
        result = pipe.check_command(dangerous, "charz-yolo-4")
        assert result.approved is False
        assert result.decision is None


# ─────────────────────────────────────────────────────────────────────────────
# Coverage 4 — HITL flow in a REAL LangGraph (no LLM; scripted tool calls)
# ─────────────────────────────────────────────────────────────────────────────
@tool("terminal")
def _fake_terminal(command: str = "", commands: list[str] | None = None) -> str:
    """Stub terminal tool: never runs anything, returns a sentinel."""
    return _SENTINEL_TERMINAL


@tool("python_repl")
def _fake_python_repl(query: str = "") -> str:
    """Stub python_repl tool: never runs anything, returns a sentinel."""
    return _SENTINEL_REPL


class _ScriptedModel(BaseChatModel):
    """Emits one scripted tool call, then idles (mirrors tests/full pattern)."""

    calls: ClassVar[int] = 0
    scripted_calls: ClassVar[list] = []

    @property
    def _llm_type(self) -> str:
        return "stub-hitl-characterization"

    def bind_tools(self, tools, **kwargs):
        # create_agent binds the registered tools; with tools=[] (the full-test
        # pattern) binding is skipped, but we also drive graphs WITH stub tools.
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


def _build_graph(scripted_calls, tools=(), hitl_config: HITLConfig | None = None):
    """Real create_agent + real HumanInTheLoop; the stub model drives it."""
    _ScriptedModel.calls = 0
    _ScriptedModel.scripted_calls = list(scripted_calls)

    hitl = HumanInTheLoop(hitl_config or HITLConfig())
    graph = create_agent(
        model=_ScriptedModel(),
        state_schema=_HarnessState,
        checkpointer=MemorySaver(),
        tools=list(tools),  # after_model intercepts BEFORE the tool node runs
        middleware=[hitl],
    )
    return graph, hitl


def _invoke(graph, thread_id: str, session_id: str):
    config = {"configurable": {"thread_id": thread_id}}
    out = graph.invoke(
        {"messages": [HumanMessage(content="please run it")], "session_id": session_id},
        config,
    )
    return out, config


def _tool_messages(out) -> list[ToolMessage]:
    return [m for m in out["messages"] if isinstance(m, ToolMessage)]


def _pending_tasks(graph, config):
    return list(graph.get_state(config).tasks or [])


class TestHitlGraphFlow:
    """core.py:276-361 after_model — locked end-to-end on a real graph."""

    def test_check_command_invoked_for_terminal_with_joined_commands(self):
        # # PRESERVE — core.py:277-280: the "commands" LIST key is extracted and
        # joined with " && " before check_command(command, session_id) is called.
        graph, hitl = _build_graph(
            scripted_calls=[
                {
                    "name": "terminal",
                    "args": {"commands": ["ls -la", "git status"]},
                    "id": "call_charz_join",
                    "type": "tool_call",
                }
            ],
            tools=[_fake_terminal],
        )
        recorded: list = []
        original = hitl.approval.check_command

        def spy(command, session_id, *args, **kwargs):
            recorded.append((command, session_id))
            return original(command, session_id)

        hitl.approval.check_command = spy  # instance attribute shadows the method

        out, config = _invoke(graph, "charz-join-1", "charz-join-1")

        assert recorded == [("ls -la && git status", "charz-join-1")]
        # approved (benign) -> the stub tool actually executed
        assert any(m.content == _SENTINEL_TERMINAL for m in _tool_messages(out))
        assert _pending_tasks(graph, config) == []

    def test_check_command_dual_key_falls_back_to_command(self):
        # # PRESERVE — core.py:277: ``tool_args.get("commands", "") or
        # tool_args.get("command", "")`` — with no "commands" key, the
        # singular "command" key feeds check_command verbatim.
        graph, hitl = _build_graph(
            scripted_calls=[
                {
                    "name": "terminal",
                    "args": {"command": "git status"},
                    "id": "call_charz_dual",
                    "type": "tool_call",
                }
            ],
            tools=[_fake_terminal],
        )
        recorded: list = []
        original = hitl.approval.check_command

        def spy(command, session_id, *args, **kwargs):
            recorded.append((command, session_id))
            return original(command, session_id)

        hitl.approval.check_command = spy

        out, config = _invoke(graph, "charz-dual-1", "charz-dual-1")

        assert recorded == [("git status", "charz-dual-1")]
        assert any(m.content == _SENTINEL_TERMINAL for m in _tool_messages(out))
        assert _pending_tasks(graph, config) == []

    def test_hardline_command_denied_with_error_tool_message_not_interrupt(self):
        # # PRESERVE — core.py:282-291: hardline deny returns a refusal
        # ToolMessage (status=error, reason + BLOCKED_MESSAGE) and the graph
        # COMPLETES with no pending interrupt task.
        graph, _hitl = _build_graph(
            scripted_calls=[
                {
                    "name": "terminal",
                    "args": {"command": "rm -rf /"},
                    "id": "call_charz_hardline",
                    "type": "tool_call",
                }
            ],
            tools=[],  # blocked in after_model; tool node never reached
        )

        out, config = _invoke(graph, "charz-hardline-1", "charz-hardline-1")

        assert _pending_tasks(graph, config) == [], "hardline deny must NOT interrupt"
        denied = [
            m
            for m in _tool_messages(out)
            if getattr(m, "name", None) == "terminal" and getattr(m, "status", None) == "error"
        ]
        assert denied, "expected a refusal ToolMessage for the hardline command"
        content = denied[0].content
        assert "Hardline blocklist" in content
        assert BLOCKED_MESSAGE in content

    def test_dangerous_command_yolo_off_persists_pending_interrupt(self):
        # # PRESERVE — core.py:311-348: a dangerous command with YOLO off raises
        # interrupt(); the GraphInterrupt propagates (core.py:345-348) and LangGraph
        # persists exactly one pending task carrying the HITLRequest payload.
        graph, _hitl = _build_graph(
            scripted_calls=[
                {
                    "name": "terminal",
                    "args": {"command": "git reset --hard"},
                    "id": "call_charz_interrupt",
                    "type": "tool_call",
                }
            ],
            tools=[],
        )

        out, config = _invoke(graph, "charz-interrupt-1", "charz-interrupt-1")

        # First invoke RETURNS normally; the interrupt is pending, not swallowed.
        swallowed = [
            m
            for m in _tool_messages(out)
            if getattr(m, "name", None) == "terminal" and "Approval interrupt failed" in (m.content or "")
        ]
        assert not swallowed, "GraphInterrupt must not be swallowed into a deny ToolMessage"

        tasks = _pending_tasks(graph, config)
        assert len(tasks) == 1, f"expected 1 pending interrupt task, got {len(tasks)}"
        assert tasks[0].name == "HumanInTheLoop.after_model"
        payload = tasks[0].interrupts[0].value
        assert payload.get("action_requests"), "payload must include action_requests"
        first_action = payload["action_requests"][0]
        assert first_action["name"] == "terminal"
        assert "git reset --hard" in first_action["args"]["command"]
        assert payload["review_configs"][0]["allowed_decisions"] == ["approve", "reject"]

    def test_interrupt_resume_approve_executes_tool(self):
        # # PRESERVE — resume mechanics: Command(resume={"decisions": [{"type":
        # "approve"}]}) resolves the pending interrupt and the tool call proceeds.
        graph, _hitl = _build_graph(
            scripted_calls=[
                {
                    "name": "terminal",
                    "args": {"command": "git reset --hard"},
                    "id": "call_charz_resume_ok",
                    "type": "tool_call",
                }
            ],
            tools=[_fake_terminal],
        )
        _out, config = _invoke(graph, "charz-resume-ok-1", "charz-resume-ok-1")
        assert len(_pending_tasks(graph, config)) == 1

        graph.invoke(
            Command(resume={"decisions": [{"type": "approve"}]}),
            config,
        )

        assert _pending_tasks(graph, config) == []
        executed = [
            m
            for m in graph.get_state(config).values.get("messages", [])
            if isinstance(m, ToolMessage)
            and getattr(m, "name", None) == "terminal"
            and m.content == _SENTINEL_TERMINAL
        ]
        assert executed, "approved tool call must have executed the stub terminal tool"
        assert getattr(executed[0], "status", None) != "error"

    def test_interrupt_resume_reject_injects_deny_tool_message(self):
        # # PRESERVE — Command(resume={"decisions": [{"type": "reject", ...}]})
        # injects "User denied: <message>. <BLOCKED_MESSAGE>" error ToolMessage.
        graph, _hitl = _build_graph(
            scripted_calls=[
                {
                    "name": "terminal",
                    "args": {"command": "git reset --hard"},
                    "id": "call_charz_resume_no",
                    "type": "tool_call",
                }
            ],
            tools=[_fake_terminal],
        )
        _out, config = _invoke(graph, "charz-resume-no-1", "charz-resume-no-1")
        assert len(_pending_tasks(graph, config)) == 1

        graph.invoke(
            Command(resume={"decisions": [{"type": "reject", "message": "No way"}]}),
            config,
        )

        assert _pending_tasks(graph, config) == []
        denied = [
            m
            for m in graph.get_state(config).values.get("messages", [])
            if isinstance(m, ToolMessage)
            and getattr(m, "name", None) == "terminal"
            and getattr(m, "status", None) == "error"
        ]
        assert denied, "rejected tool call must produce a deny ToolMessage"
        content = denied[0].content
        assert "User denied" in content
        assert "No way" in content
        assert BLOCKED_MESSAGE in content

    def test_yolo_on_dangerous_command_direct_pass(self):
        # # PRESERVE — YOLO config (HITLConfig(yolo_mode=True), as existing unit
        # tests construct it) approves INSIDE check_command: no interrupt, no deny
        # message; the dangerous call goes straight to execution.
        graph, hitl = _build_graph(
            scripted_calls=[
                {
                    "name": "terminal",
                    "args": {"command": "git reset --hard"},
                    "id": "call_charz_yolo_graph",
                    "type": "tool_call",
                }
            ],
            tools=[_fake_terminal],
            hitl_config=HITLConfig(yolo_mode=True),
        )
        recorded: list = []
        original = hitl.approval.check_command

        def spy(command, session_id, *args, **kwargs):
            recorded.append(command)
            return original(command, session_id)

        hitl.approval.check_command = spy

        out, config = _invoke(graph, "charz-yolo-graph-1", "charz-yolo-graph-1")

        assert recorded == ["git reset --hard"], "check_command is still called under YOLO"
        assert _pending_tasks(graph, config) == [], "YOLO must not interrupt"
        assert any(m.content == _SENTINEL_TERMINAL for m in _tool_messages(out))
        assert not [
            m for m in _tool_messages(out) if getattr(m, "status", None) == "error"
        ], "YOLO pass must not produce a deny/error ToolMessage"

    def test_python_repl_not_intercepted_by_hitl(self):
        # # WILL-CHANGE(Task 8) — TODAY python_repl falls through after_model to
        # the allow-through plugin-tool-approval layer (core.py:469): no
        # check_command call, no interrupt, straight to execution — even for a
        # shell-destructive-looking payload. Task 8 routes python_repl (with
        # sandbox bypass requests) through HITL approval; that task updates this
        # assertion (e.g. sandbox=False -> interrupt while sandbox=True still passes).
        graph, hitl = _build_graph(
            scripted_calls=[
                {
                    "name": "python_repl",
                    "args": {"query": "import shutil; shutil.rmtree('/')"},
                    "id": "call_charz_repl_skip",
                    "type": "tool_call",
                }
            ],
            tools=[_fake_python_repl],
        )
        recorded: list = []
        original = hitl.approval.check_command

        def spy(command, session_id, *args, **kwargs):
            recorded.append(command)
            return original(command, session_id)

        hitl.approval.check_command = spy

        out, config = _invoke(graph, "charz-repl-skip-1", "charz-repl-skip-1")

        assert recorded == [], "python_repl must not reach check_command today"
        assert _pending_tasks(graph, config) == [], "python_repl must not interrupt today"
        assert any(m.content == _SENTINEL_REPL for m in _tool_messages(out))


# ─────────────────────────────────────────────────────────────────────────────
# Coverage 5 — handle_tool_error=True semantics (ToolException -> error channel)
# ─────────────────────────────────────────────────────────────────────────────
class TestHandleToolErrorSemantics:
    """terminal.py:128 / python_repl.py:128 — handle_tool_error=True set at build."""

    def test_built_tools_have_handle_tool_error_true_and_stable_names(self):
        # # PRESERVE — both builders set handle_tool_error=True and keep the
        # tool names the HITL middleware dispatches on ("terminal" / "python_repl").
        terminal = build_terminal_tool()
        repl = build_python_repl_tool()
        assert terminal.handle_tool_error is True
        assert repl.handle_tool_error is True
        assert terminal.name == "terminal"
        assert repl.name == "python_repl"

    @pytest.mark.parametrize("kind", ["terminal", "python_repl"])
    def test_tool_exception_becomes_error_string_not_raise(self, kind, monkeypatch):
        # # PRESERVE — the semantics the agent tool node relies on: with
        # handle_tool_error=True, a ToolException raised inside _run is converted
        # to an "Error: ..." string (rendered as an error ToolMessage in the
        # graph) instead of propagating. Simulated here by patching _run.
        def boom(self, *args, **kwargs):
            raise ToolException("boom-characterization")

        if kind == "terminal":
            monkeypatch.setattr(SafeShellTool, "_run", boom)
            built = build_terminal_tool()
            payload = {"commands": ["echo hi"]}
        else:
            monkeypatch.setattr(TimedPythonREPLTool, "_run", boom)
            built = build_python_repl_tool()
            payload = {"query": "print(1)"}

        out = built.invoke(payload)
        assert isinstance(out, str), "ToolException must be converted, not raised"
        assert "boom-characterization" in out
