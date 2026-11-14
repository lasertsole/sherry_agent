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


class _StubLLM:
    """Records ``bind`` calls; ``create_agent`` receives the bound marker."""

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    def bind(self, **kwargs: Any) -> SimpleNamespace:
        self._calls.append(kwargs)
        return SimpleNamespace(kind="bound", kwargs=kwargs)

    def with_request_options(self, options: dict[str, Any]) -> SimpleNamespace:
        self._calls.append(options)
        return SimpleNamespace(kind="bound", kwargs=options)


@pytest.fixture()
def create_agent_capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the heavy dependencies of ``_build_child_agent``; capture its call."""
    captured: dict[str, Any] = {
        "main_llm_calls": [],
        "profile_calls": [],
        "aux_llm_calls": [],
        "bind_calls": [],
    }

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
    monkeypatch.setattr(
        "models.build_main_llm",
        lambda *a, **k: captured["main_llm_calls"].append(k) or _StubLLM(captured["bind_calls"]),
    )
    monkeypatch.setattr(
        "models.build_main_llm_for_profile",
        lambda **k: captured["profile_calls"].append(k) or _StubLLM(captured["bind_calls"]),
    )
    monkeypatch.setattr(
        "models.build_auxiliary_llm",
        lambda *a, **k: captured["aux_llm_calls"].append(k) or _StubLLM(captured["bind_calls"]),
    )
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


class TestStructuredOutputLadder:
    """Default level 0 is untouched; degradation only narrows the strategy.

    The runner escalates on a provider rejection (see the spawn retry loop) and
    PERSISTS the level per model, so these tests pin what each level builds.
    """

    def test_level_0_keeps_langchains_structured_output(self, create_agent_capture):
        _build(output_schema=_SCHEMA, role=SubagentSessionRole.ORCHESTRATOR)

        assert create_agent_capture["response_format"] == _SCHEMA
        assert create_agent_capture["bind_calls"] == []
        # Thinking is NOT touched at level 0.
        assert create_agent_capture["main_llm_calls"] == [{}]

    def test_level_1_sends_json_object_itself_and_drops_response_format(self, create_agent_capture):
        _build(
            output_schema=_SCHEMA,
            role=SubagentSessionRole.ORCHESTRATOR,
            strategy_level=1,
        )

        assert create_agent_capture["response_format"] is None
        assert create_agent_capture["bind_calls"] == [{"response_format": {"type": "json_object"}}]
        assert create_agent_capture["model"].kwargs == {"response_format": {"type": "json_object"}}
        # Thinking still untouched at level 1.
        assert create_agent_capture["main_llm_calls"] == [{}]

    def test_level_2_adds_thinking_off(self, create_agent_capture):
        _build(
            output_schema=_SCHEMA,
            role=SubagentSessionRole.ORCHESTRATOR,
            strategy_level=2,
        )

        assert create_agent_capture["response_format"] is None
        assert create_agent_capture["main_llm_calls"] == [{"thinking": False}]

    def test_level_2_disables_thinking_for_the_auxiliary_family_too(self, create_agent_capture):
        _build(output_schema=_SCHEMA, strategy_level=2)

        assert create_agent_capture["aux_llm_calls"] == [{"thinking": False}]

    def test_level_2_profile_build_pins_thinking_off(self, create_agent_capture):
        _build(
            output_schema=_SCHEMA,
            strategy_level=2,
            model_profile={"model": "deepseek-flash", "provider": "deepseek"},
        )

        assert create_agent_capture["profile_calls"][0]["thinking"] is False

    def test_a_free_form_child_never_binds_json_mode(self, create_agent_capture):
        _build(role=SubagentSessionRole.ORCHESTRATOR, strategy_level=2)

        assert create_agent_capture["bind_calls"] == []
        assert create_agent_capture["main_llm_calls"] == [{}]

    def test_a_free_form_profile_child_leaves_thinking_to_the_env(self, create_agent_capture):
        _build(model_profile={"model": "deepseek-flash", "provider": "deepseek"})

        # Byte-identical call shape for the free-form path: no thinking key.
        assert "thinking" not in create_agent_capture["profile_calls"][0]


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


class TestDegradationTrigger:
    """Only structured-output rejections degrade a model; other errors surface."""

    def test_the_known_deepseek_rejection_matches(self):
        from agent.tools.subagent.spawn.core import _is_structured_output_rejection

        exc = RuntimeError(
            "Error code: 400 - {'error': {'message': 'Thinking mode does not support "
            "this tool_choice (request_id: x)'}}"
        )
        assert _is_structured_output_rejection(exc) is True

    def test_other_provider_errors_do_not_degrade(self):
        from agent.tools.subagent.spawn.core import _is_structured_output_rejection

        assert _is_structured_output_rejection(RuntimeError("connection reset")) is False
        assert _is_structured_output_rejection(TimeoutError("timeout")) is False

    def test_a_transient_error_mentioning_the_strategy_does_not_degrade(self):
        from agent.tools.subagent.spawn.core import _is_structured_output_rejection

        assert (
            _is_structured_output_rejection(
                RuntimeError("429 rate limit exceeded while processing response_format")
            )
            is False
        )
        assert (
            _is_structured_output_rejection(
                RuntimeError("Too many requests: structured output unavailable temporarily")
            )
            is False
        )

    def test_a_genuine_400_still_degrades(self):
        from agent.tools.subagent.spawn.core import _is_structured_output_rejection

        assert (
            _is_structured_output_rejection(
                RuntimeError("400 This model does not support response_format")
            )
            is True
        )

    def test_the_identity_mirrors_the_selection_precedence(self, monkeypatch):
        from agent.tools.subagent.spawn.core import _child_model_identity
        from agent.tools.subagent.types import FunctionalRole

        monkeypatch.setenv("AUXILIARY_LLM_PROVIDER", "deepseek")
        monkeypatch.setenv("AUXILIARY_LLM_API_NAME", "deepseek-flash")

        assert _child_model_identity(
            {"model": "glm-4.6", "provider": "zhipu"},
            FunctionalRole.RESEARCHER,
            "auxiliary",
            SubagentSessionRole.LEAF,
        ) == ("zhipu", "glm-4.6")
        assert _child_model_identity(
            None, FunctionalRole.RESEARCHER, "auxiliary", SubagentSessionRole.LEAF
        ) == ("deepseek", "deepseek-flash")
