"""Hermetic end-to-end: an invalid tool call no longer ends the turn.

Drives a REAL ``create_agent`` graph (model → tool routing included) with a
scripted fake model, on the exact failure shape from SWE-bench run
20261017_003529: the model's only call has unquoted, unparseable JSON
arguments. Without the middleware the turn ends with a narration-only answer
and the tool never runs (the pre-fix behavior); with
``InvalidToolCallRepairMiddleware`` the call is repaired at the model seam, the
tool executes, and the loop continues to the model's final answer.

Chain membership of the middleware itself is pinned separately by
``tests/agent/core/test_middleware_order.py`` and
``tests/agent/middlewares/test_scaffolding.py``.
"""

import asyncio
import sys
import types

import pytest

# ---- llama_cpp stub before any agent import (repo convention).
_llama_stub = types.ModuleType("llama_cpp")
_llama_stub.Llama = type("Llama", (), {})
sys.modules.setdefault("llama_cpp", _llama_stub)
_llama_chat_sub = types.ModuleType("llama_cpp.llama_chat_format")
_llama_chat_sub.Qwen25VLChatHandler = type("Qwen25VLChatHandler", (), {})
sys.modules.setdefault("llama_cpp.llama_chat_format", _llama_chat_sub)

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from agent.middlewares.invalid_tool_call_repair import InvalidToolCallRepairMiddleware

pytestmark = [pytest.mark.module, pytest.mark.timeout(120)]

#: The REAL invalid arguments string from the django run (checkpoint evidence).
RAW_ARGS = '{"commands": cd $(pwd) && pwd && git log --oneline -3 && ls}'


@tool("shell_like")
def shell_like(commands: str) -> str:
    """Run a shell command (test stand-in for ``terminal``)."""
    return f"ran: {commands}"


class _ScriptedModel(GenericFakeChatModel):
    """GenericFakeChatModel that skips tool binding (scripted messages only)."""

    def bind_tools(self, tools, **kwargs):  # type: ignore[override]
        return self


def _invalid_message() -> AIMessage:
    return AIMessage(
        content="Now let me check the git log for the upstream fix.",
        tool_calls=[],
        invalid_tool_calls=[
            {
                "type": "invalid_tool_call",
                "id": "call_00_eHoDiI1ziSsKEnzYmMmi4767",
                "name": "shell_like",
                "args": RAW_ARGS,
                "error": None,
            }
        ],
    )


def _run(agent, text: str = "investigate the bug"):
    return asyncio.run(agent.ainvoke({"messages": [HumanMessage(content=text)]}))


def test_without_repair_the_turn_ends_without_running_the_tool():
    """The pre-fix behavior this middleware exists to remove."""
    agent = create_agent(
        model=_ScriptedModel(messages=iter([_invalid_message()])), tools=[shell_like]
    )
    out = _run(agent)

    assert not [m for m in out["messages"] if m.type == "tool"]
    assert out["messages"][-1].content.startswith("Now let me check the git log")


def test_with_repair_the_tool_runs_and_the_turn_continues():
    final = AIMessage(content="Done — the log shows the upstream fix.")
    agent = create_agent(
        model=_ScriptedModel(messages=iter([_invalid_message(), final])),
        tools=[shell_like],
        middleware=[InvalidToolCallRepairMiddleware()],
    )
    out = _run(agent)

    tool_msgs = [m for m in out["messages"] if m.type == "tool"]
    assert tool_msgs, "the repaired call must reach the ToolNode"
    assert "ran: cd $(pwd) && pwd && git log --oneline -3 && ls" in str(tool_msgs[0].content)

    # The repaired AI message in state carries a VALID call with the original id.
    repaired = next(
        m for m in out["messages"] if isinstance(m, AIMessage) and getattr(m, "tool_calls", None)
    )
    assert repaired.tool_calls[0]["id"] == "call_00_eHoDiI1ziSsKEnzYmMmi4767"
    assert repaired.tool_calls[0]["name"] == "shell_like"

    # The loop continued: the model's next scripted answer is the final message.
    assert out["messages"][-1].content == "Done — the log shows the upstream fix."
