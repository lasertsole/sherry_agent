"""Gap C: structured output is enforced at generation time, not only after.

``_build_child_agent`` hands the step's schema to ``create_agent`` as
``response_format`` (a raw schema → LangChain's AutoStrategy: the provider's
native structured output where supported, else a structured-output tool call),
while the schema text in the system prompt stays as the prompt-level fallback
and Tier 1 re-validates on resume. ``sessions_spawn`` exposes the same schema
to the LLM for tool-driven children, so both spawn doors agree.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from agent.tools.subagent.spawn.core import _build_child_agent
from agent.tools.subagent.types import SubagentSessionRole

pytestmark = [pytest.mark.unit]

_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


class _StubCheckpointer:
    async def setup(self) -> None:
        return None


@pytest.fixture()
def create_agent_capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the heavy dependencies of ``_build_child_agent``; capture its call."""
    captured: dict[str, Any] = {}

    def _fake_create_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(name="child-graph")

    async def _fake_checkpointer(*args, **kwargs):
        return _StubCheckpointer()

    monkeypatch.setattr("langchain.agents.create_agent", _fake_create_agent)
    monkeypatch.setattr("agent.checkpointer.build_async_sqlite_checkpointer", _fake_checkpointer)
    # CI runs without .env: pin the token floor deterministically.
    monkeypatch.setenv("MAIN_LLM_MAX_TOKEN", "200000")
    monkeypatch.setenv("AUXILIARY_LLM_MAX_TOKEN", "200000")
    monkeypatch.setattr("models.LLMs.main_llm.max_tokens", 65_536)
    monkeypatch.setattr("models.build_main_llm", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("models.build_auxiliary_llm", lambda *a, **k: SimpleNamespace())
    return captured


def _build(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "system_prompt": "child",
        "tools": [],
        "tool_allow": None,
        "tool_deny": None,
        "role": SubagentSessionRole.LEAF,
    }
    kwargs.update(overrides)
    asyncio.run(_build_child_agent(**kwargs))
    return kwargs


class TestResponseFormat:
    def test_the_schema_is_handed_to_create_agent(self, create_agent_capture):
        _build(output_schema=_SCHEMA)

        assert create_agent_capture["response_format"] == _SCHEMA, (
            "the schema must reach create_agent as a generation-time constraint"
        )

    def test_a_child_without_a_schema_keeps_free_form_output(self, create_agent_capture):
        _build()

        assert create_agent_capture["response_format"] is None

    def test_the_prompt_level_schema_text_still_rides_along(self, create_agent_capture):
        # The system prompt keeps its schema guidance: it is the fallback for a
        # provider that resolves AutoStrategy to a structured-output TOOL (which
        # the model is not obliged to call).
        from agent.tools.subagent.swarm.collector import build_structured_output_prompt

        prompt = f"child\n\n{build_structured_output_prompt(_SCHEMA)}"
        _build(system_prompt=prompt, output_schema=_SCHEMA)

        assert "summary" in create_agent_capture["system_prompt"]


class TestSessionsSpawnSurfaces:
    """Both spawn doors pass the schema downstream."""

    @pytest.mark.asyncio
    async def test_the_runtime_tool_forwards_the_schema(self, monkeypatch: pytest.MonkeyPatch):
        from agent.tools.subagent.tools import runtime_tools

        captured: dict[str, Any] = {}

        async def _fake_spawn(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                status="accepted",
                run_id="run-1",
                child_session_key="agent:main:subagent:x",
                task_name=None,
                note=None,
                error=None,
            )

        monkeypatch.setattr(runtime_tools, "_session_key", lambda sid: "agent:main:session:s1")
        monkeypatch.setattr(
            "agent.tools.subagent.spawn.spawn_subagent_direct", _fake_spawn, raising=False
        )
        monkeypatch.setattr(
            "agent.tools.subagent.spawn.privilege.check_spawn_permission",
            lambda sid: (True, ""),
        )

        out = await runtime_tools.sessions_spawn_runtime_tool.ainvoke(
            {"task": "report", "session_id": "s1", "output_schema": _SCHEMA}
        )

        assert "status=accepted" in out
        assert captured["output_schema"] == _SCHEMA

    @pytest.mark.asyncio
    async def test_the_runtime_tool_defaults_the_schema_to_none(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from agent.tools.subagent.tools import runtime_tools

        captured: dict[str, Any] = {}

        async def _fake_spawn(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                status="accepted",
                run_id="run-1",
                child_session_key="agent:main:subagent:x",
                task_name=None,
                note=None,
                error=None,
            )

        monkeypatch.setattr(runtime_tools, "_session_key", lambda sid: "agent:main:session:s1")
        monkeypatch.setattr(
            "agent.tools.subagent.spawn.spawn_subagent_direct", _fake_spawn, raising=False
        )
        monkeypatch.setattr(
            "agent.tools.subagent.spawn.privilege.check_spawn_permission",
            lambda sid: (True, ""),
        )

        await runtime_tools.sessions_spawn_runtime_tool.ainvoke(
            {"task": "report", "session_id": "s1"}
        )

        assert captured["output_schema"] is None

    def test_both_tool_schemas_declare_the_field(self):
        from agent.tools.subagent.tools.runtime_tools import sessions_spawn_runtime_tool
        from agent.tools.subagent.tools.sessions_spawn import SessionsSpawnSchema

        assert "output_schema" in sessions_spawn_runtime_tool.args
        assert "output_schema" in SessionsSpawnSchema.model_fields
