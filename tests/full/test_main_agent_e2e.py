"""Level C: Real main-agent E2E test against the live configured LLM.

PURPOSE
-------
``tests/integration/test_real_e2e.py`` drives the subagent executor with a real
LLM, but it never exercises the **main agent graph** — i.e. the actual
``create_agent``/``langgraph`` path the server uses. This test closes that gap:
it builds a minimal ``create_agent`` graph whose model is the **live
``build_main_llm()``** (currently resolved from ``.env`` →
``MAIN_LLM_PROVIDER=openai`` + ``MAIN_LLM_NAME=glm-5.3-flash`` +
``MAIN_LLM_API_BASE=https://open.bigmodel.cn/api/paas/v4/``, i.e. the Zhipu
BigModel OpenAI-compatible endpoint), then drives a real ``ainvoke`` and asserts
the graph returns a genuine, non-empty assistant reply.

This is the single most direct regression test for the "switch the main model"
change: if ``build_main_llm()`` can't reach the endpoint, fails auth, or the
provider/base-url combo is misconfigured, this test fails loudly with the real
provider error instead of silently building a stub.

REQUIRES
--------
- ``uv run pytest`` (the uv venv has ``llama_cpp``; system ``python`` does not).
- A populated ``.env`` with a valid ``MAIN_LLM_API_KEY`` for the configured
  endpoint. No network mocking — this is a live integration test.
"""

from __future__ import annotations

import pytest

from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from models import build_main_llm


pytestmark = pytest.mark.unit


class _E2EState(AgentState):
    """Tiny state schema carrying a session_id, mirroring the server graph."""

    session_id: str

    def __init__(self, **data):
        # Allow state without a session_id for fetch: the server always
        # supplies one, so require it by leaving the field un-defaulted.
        super().__init__(**data)


TEST_PROMPT = (
    "Reply with exactly the two words 'agent ok' and nothing else. "
    "No reasoning, no extra text, no punctuation."
)


def _build_graph():
    """Build a minimal real agent graph bound to the live main LLM."""
    model = build_main_llm()
    graph = create_agent(
        model=model,
        state_schema=_E2EState,
        checkpointer=MemorySaver(),
        tools=[],  # Pure chat turn — no tool-firing, minimizes risk
    )
    return graph


@pytest.mark.asyncio
async def test_main_agent_returns_live_nonempty_reply() -> None:
    """A real ainvoke through build_main_llm() must yield non-empty output.

    This exercises the *main llm* → *real endpoint* → *real graph* path for the
    configured provider (glm-5.3-flash via Zhipu). A failure here means the
    .env model switch is broken at the network/auth layer, not just at import.
    """
    graph = _build_graph()

    config = {"configurable": {"thread_id": "main-agent-e2e-live"}}
    out = await graph.ainvoke(
        {"messages": [HumanMessage(content=TEST_PROMPT)], "session_id": "main-agent-e2e-live"},
        config,
    )

    # Collect every assistant-authored piece of content produced across the run.
    messages = out.get("messages") or []
    assistant_text = ""
    for m in messages:
        if getattr(m, "type", None) == "ai":
            content = getattr(m, "content", "") or ""
            if isinstance(content, str):
                assistant_text += content

    assert assistant_text.strip(), (
        "The real main-LLM agent returned an EMPTY assistant reply. "
        "This means the configured endpoint/model did not produce usable "
        "output — check MAIN_LLM_PROVIDER/NAME/API_BASE/API_KEY in .env "
        f"and the endpoint reachability. Full final state messages: {messages!r}"
    )

    normalized = assistant_text.strip().lower().replace(".", "")
    assert "agent ok" in normalized, (
        f"Unexpected reply from the live main agent.\n"
        f"expected substring: 'agent ok'\n"
        f"got: {assistant_text!r}"
    )
