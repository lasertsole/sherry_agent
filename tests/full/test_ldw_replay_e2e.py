"""Real main-agent E2E: the agent can invoke the workflow_replay tool.

The tool itself is the formal integration point; this test proves a live model
selects it and receives its JSON status payload. It does not run a workflow
(that requires a compiled plan DAG); the deterministic replay path is covered
by tests/agent/tools/ldw/.
"""

from __future__ import annotations

import json
import uuid

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from agent.tools.ldw.tool import build_workflow_replay_tool
from models import build_main_llm


class _E2EState(AgentState):
    session_id: str


pytestmark = [pytest.mark.llm_e2e, pytest.mark.system, pytest.mark.timeout(300)]


@pytest.mark.asyncio
async def test_main_agent_can_call_workflow_replay_status() -> None:
    session_id = f"e2e-ldw-{uuid.uuid4().hex[:10]}"
    graph = create_agent(
        model=build_main_llm(temperature=0),
        state_schema=_E2EState,
        checkpointer=MemorySaver(),
        tools=[build_workflow_replay_tool()],
    )
    prompt = (
        "Call the `workflow_replay` tool with action='status' and nothing else. "
        "Then report the exact JSON object it returned."
    )
    out = await graph.ainvoke(
        {"messages": [HumanMessage(content=prompt)], "session_id": session_id},
        {"configurable": {"thread_id": session_id}},
    )
    tool_texts = [
        str(message.content)
        for message in (out.get("messages") or [])
        if getattr(message, "type", None) == "tool"
    ]
    assert tool_texts, f"the live agent did not call workflow_replay: {out!r}"
    payload = json.loads(tool_texts[-1])
    assert payload["ok"] is True, payload
    assert payload["plan_key"], payload
