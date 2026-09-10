"""HITL confirmation gate for clawhub remote-npm execution.

The clawhub skill runs through the ``terminal`` tool: either the Python helper
(``...clawhub.scripts.run_clawhub_command``) or a direct
``npx --yes clawhub@latest`` invocation. Both execute remote npm code, so the
HITL terminal handler must require explicit human approval — SMART
auto-approval and the benign-command fast path must not bypass the gate.

Covered scenarios:
* detection matches the documented invocation forms and ignores other commands;
* a clawhub command that would auto-approve under SMART still interrupts;
* in a real LangGraph the call stays pending until the user decides:
  a reject prevents the npm run, an approve lets it proceed.

The ``npx`` subprocess is mocked — no npm/network call ever happens.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agent.middlewares.humanInTheLoop import (
    ApprovalMode,
    HITLConfig,
    HumanInTheLoop,
    detect_clawhub_command,
)
from agent.middlewares.humanInTheLoop import strategies
from agent.middlewares.humanInTheLoop.strategies import (
    ApprovalContext,
    ApprovalOutcome,
    TerminalApprovalHandler,
)
from agent.middlewares.humanInTheLoop.types import SmartApprovalResult
from skills.builtin.core.clawhub.scripts import clawhub_runner

pytestmark = [pytest.mark.module]

_CLAWHUB_COMMAND = (
    'python -c "from skills.builtin.core.clawhub.scripts import run_clawhub_command; '
    "run_clawhub_command(['list'])\""
)


class TestDetectClawhubCommand:
    @pytest.mark.parametrize(
        "command",
        [
            "npx --yes clawhub@latest search web scraping",
            "npx --yes clawhub@latest install my-skill --workdir .",
            'python -c "from skills.builtin.core.clawhub.scripts import run_clawhub_command"',
            'python -c "from skills.builtin.core.clawhub.scripts.clawhub_runner import run_clawhub_command"',
            "clawhub@latest update --all",
        ],
    )
    def test_matches_remote_execution_forms(self, command):
        assert detect_clawhub_command(command) == "clawhub_remote_npm"

    @pytest.mark.parametrize(
        "command",
        ["ls -la", "git status", "python -c \"print('hello')\"", "npx --yes prettier --write ."],
    )
    def test_ignores_unrelated_commands(self, command):
        assert detect_clawhub_command(command) is None


class TestSmartApprovalCannotBypassClawhub:
    def test_clawhub_interrupts_despite_smart_approve(self, monkeypatch):
        monkeypatch.delenv("SHERRY_YOLO_MODE", raising=False)
        hitl = HumanInTheLoop(HITLConfig(mode=ApprovalMode.SMART))
        ctx = ApprovalContext(
            mw=hitl,
            state={},
            runtime=None,
            session_id="clawhub-smart",
            outcome=ApprovalOutcome(),
        )
        smart_calls: list[str] = []
        monkeypatch.setattr(
            hitl.approval,
            "smart_approve",
            lambda command: (smart_calls.append(command), SmartApprovalResult.APPROVE)[1],
        )
        requested: dict = {}

        def fake_interrupt(request):
            requested["request"] = request
            return {"decisions": [{"type": "reject", "message": "no remote code"}]}

        monkeypatch.setattr(strategies, "interrupt", fake_interrupt)

        tool_call = {
            "name": "terminal",
            "args": {"command": "npx publish && npx --yes clawhub@latest search x"},
            "id": "call_clawhub_smart",
            "type": "tool_call",
        }
        consumed = TerminalApprovalHandler().handle(tool_call, ctx)

        assert consumed is True
        assert smart_calls == [], "SMART auto-approval must not run for clawhub"
        assert "request" in requested, "clawhub must raise a human interrupt"
        action = requested["request"]["action_requests"][0]
        assert action["name"] == "terminal"
        assert "clawhub remote npm execution" in action["description"]
        assert ctx.outcome.revised_tool_calls == []
        assert ctx.outcome.artificial_tool_messages, "reject must inject a deny ToolMessage"


@tool("terminal")
def _clawhub_terminal(command: str = "") -> str:
    """Terminal stub: really invokes the clawhub runner (npx subprocess mocked)."""
    result = clawhub_runner.run_clawhub_command(["list", "--workdir", "{{ROOT_DIR}}"])
    return f"executed:{result['success']}"


class _ScriptedModel(BaseChatModel):
    calls: ClassVar[int] = 0
    scripted_calls: ClassVar[list] = []

    @property
    def _llm_type(self) -> str:
        return "stub-clawhub-hitl"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        type(self).calls += 1
        if type(self).calls == 1 and type(self).scripted_calls:
            msg = AIMessage(content="", tool_calls=list(type(self).scripted_calls))
        else:
            msg = AIMessage(content="Done.")
        return ChatResult(generations=[ChatGeneration(message=msg)])


class _HarnessState(AgentState):
    session_id: str


def _build_graph(hitl_config: HITLConfig | None = None):
    _ScriptedModel.calls = 0
    _ScriptedModel.scripted_calls = [
        {
            "name": "terminal",
            "args": {"command": _CLAWHUB_COMMAND},
            "id": "call_clawhub_1",
            "type": "tool_call",
        }
    ]
    hitl = HumanInTheLoop(hitl_config or HITLConfig())
    graph = create_agent(
        model=_ScriptedModel(),
        state_schema=_HarnessState,
        checkpointer=MemorySaver(),
        tools=[_clawhub_terminal],
        middleware=[hitl],
    )
    return graph


def _invoke(graph, thread_id: str):
    config = {"configurable": {"thread_id": thread_id}}
    out = graph.invoke(
        {"messages": [HumanMessage(content="install a skill")], "session_id": thread_id},
        config,
    )
    return out, config


@pytest.fixture
def npx_calls(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(clawhub_runner.subprocess, "run", fake_run)
    return calls


class TestClawhubGraphGate:
    def test_pending_reject_never_runs_npx(self, npx_calls):
        graph = _build_graph()
        _out, config = _invoke(graph, "clawhub-reject")

        tasks = list(graph.get_state(config).tasks or [])
        assert len(tasks) == 1, "clawhub execution must await human approval"
        payload = tasks[0].interrupts[0].value
        assert "clawhub remote npm execution" in payload["action_requests"][0]["description"]
        assert npx_calls == [], "no approval yet -> npm must not run"

        graph.invoke(Command(resume={"decisions": [{"type": "reject", "message": "no"}]}), config)

        assert npx_calls == [], "rejected approval must prevent the npm execution"
        denied = [
            m
            for m in graph.get_state(config).values.get("messages", [])
            if isinstance(m, ToolMessage) and getattr(m, "status", None) == "error"
        ]
        assert denied, "reject must surface a deny ToolMessage"

    def test_approve_runs_npx(self, npx_calls):
        graph = _build_graph()
        _out, config = _invoke(graph, "clawhub-approve")
        assert list(graph.get_state(config).tasks or []), "expected a pending interrupt"

        graph.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)

        assert len(npx_calls) == 1, "approved call must run npx exactly once"
        assert npx_calls[0][:3] == ["npx", "--yes", "clawhub@latest"]
