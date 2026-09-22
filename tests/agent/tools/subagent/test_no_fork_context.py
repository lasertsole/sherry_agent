"""Regression lock: the FORK context mode and its ``context`` parameter are gone.

Sub-agents always run with an independent (isolated) context. These tests pin
the removal at each layer: the spawn entry point signature, the run record and
config schemas, and both ``sessions_spawn`` tool surfaces — an incoming
``context`` key is silently ignored (Pydantic's default for unknown fields) and
never reaches the spawn pipeline.
"""

import inspect

import pytest

pytestmark = [pytest.mark.unit]


def _accepted():
    from agent.tools.subagent.spawn.core import SpawnResult

    return SpawnResult(status="accepted", run_id="r", child_session_key="c")


def test_spawn_entrypoint_has_no_context_params():
    from agent.tools.subagent.spawn.core import spawn_subagent_direct

    params = set(inspect.signature(spawn_subagent_direct).parameters)
    assert "context" not in params
    assert "context_mode" not in params
    assert "requester_session_id" not in params


def test_run_record_has_no_context_mode_field():
    from agent.tools.subagent.types.registry import SubagentRunRecord

    assert "context_mode" not in SubagentRunRecord.model_fields


def test_config_has_no_default_context_mode():
    from agent.tools.subagent.config import SubagentConfig

    assert "default_context_mode" not in SubagentConfig.model_fields


def test_build_time_schema_has_no_context_field():
    from agent.tools.subagent.tools.sessions_spawn import SessionsSpawnSchema

    assert "context" not in SessionsSpawnSchema.model_fields


def test_runtime_tool_schema_has_no_context_field():
    from agent.tools.subagent.tools.runtime_tools import sessions_spawn_runtime_tool

    assert "context" not in sessions_spawn_runtime_tool.args


class TestSpawnToolsIgnoreContextKey:
    @pytest.mark.asyncio
    async def test_build_time_tool_ignores_context(self, monkeypatch):
        from agent.tools.subagent.tools import sessions_spawn as mod

        captured: dict = {}

        async def _fake_spawn(**kwargs):
            captured.update(kwargs)
            return _accepted()

        monkeypatch.setattr(mod, "spawn_subagent_direct", _fake_spawn)
        out = await mod.SessionsSpawnTool(session_id="s-1").ainvoke(
            {"task": "do", "context": "fork"}
        )

        assert "accepted" in out
        assert captured["task"] == "do"
        assert "context" not in captured
        assert "context_mode" not in captured

    @pytest.mark.asyncio
    async def test_runtime_tool_ignores_context(self, monkeypatch):
        import agent.tools.subagent.spawn as spawn_pkg
        from agent.tools.subagent.tools import runtime_tools as rt

        captured: dict = {}

        async def _fake_spawn(**kwargs):
            captured.update(kwargs)
            return _accepted()

        monkeypatch.setattr(spawn_pkg, "spawn_subagent_direct", _fake_spawn)
        out = await rt.sessions_spawn_runtime_tool.ainvoke(
            {"task": "do", "session_id": "s-1", "context": "fork"}
        )

        assert "accepted" in out
        assert captured["task"] == "do"
        assert "context" not in captured
        assert "context_mode" not in captured
