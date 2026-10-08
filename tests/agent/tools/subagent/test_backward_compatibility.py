"""T1.5: default-path backward compatibility for the functional-role feature."""

import asyncio

import pytest

from agent.tools.subagent.config import get_config
from agent.tools.subagent.registry import clear as clear_registry
from agent.tools.subagent.spawn import core as spawn_core
from agent.tools.subagent.spawn.core import spawn_subagent_direct
from agent.tools.subagent.types import FunctionalRole, SubagentRunRecord

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture
def _captured_lane(monkeypatch):
    captured: list[dict] = []

    async def _fake_lane(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(spawn_core, "_execute_subagent_with_lane", _fake_lane)
    clear_registry()
    yield captured
    clear_registry()


def _spawn(captured: list[dict], **kwargs):
    async def _go():
        result = await spawn_subagent_direct(
            task="do the task",
            requester_session_key="agent:main:session:test",
            **kwargs,
        )
        # Let the fake lane task run on the same loop before it closes.
        await asyncio.sleep(0.01)
        return result

    return asyncio.run(_go())


class TestDefaultSpawnUnchanged:
    def test_default_spawn_keeps_general_and_depth_policy(self, _captured_lane):
        result = _spawn(_captured_lane)
        assert result.status == "accepted"
        lane = _captured_lane[-1]
        run = lane["run"]
        assert run.functional_role is FunctionalRole.GENERAL
        assert lane["model_tier"] is None
        assert run.inherited_tool_allow == []
        assert run.inherited_tool_deny == []

    def test_default_prompt_has_no_functional_role_section(self, _captured_lane):
        _spawn(_captured_lane)
        prompt = _captured_lane[-1]["system_prompt"]
        assert "specialization" not in prompt
        assert "## Role Instructions" not in prompt

    def test_explicit_general_hint_is_inert(self, _captured_lane, monkeypatch):
        monkeypatch.setattr(get_config(), "max_spawn_depth", 1)
        _spawn(_captured_lane, functional_role_hint="general")
        lane = _captured_lane[-1]
        assert lane["run"].functional_role is FunctionalRole.GENERAL
        assert lane["model_tier"] is None
        assert lane["run"].inherited_tool_allow == []

    def test_role_decorates_prompt_when_selected(self, _captured_lane, monkeypatch):
        monkeypatch.setattr(get_config(), "max_spawn_depth", 1)
        _spawn(_captured_lane, functional_role_hint="researcher")
        prompt = _captured_lane[-1]["system_prompt"]
        assert "RESEARCHER specialization" in prompt
        assert "## Role Instructions" in prompt


class TestAgentIdCompatibility:
    def test_default_agent_id_names_session_key_only(self, _captured_lane):
        result = _spawn(_captured_lane, agent_id="main")
        assert result.child_session_key.startswith("agent:main:subagent:")
        assert _captured_lane[-1]["run"].agent_id == "main"

    def test_legacy_record_without_field_deserializes_to_general(self):
        legacy = {
            "run_id": "r",
            "child_session_key": "agent:main:subagent:x",
            "requester_session_key": "agent:main:session:p",
            "task": "t",
        }
        assert SubagentRunRecord.model_validate(legacy).functional_role is FunctionalRole.GENERAL
