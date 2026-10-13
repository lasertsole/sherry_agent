"""Unit tests for the SkillWriteProvider half of runtime.data_provider.

Covers the register/get contract (identity, last-writer-wins override),
missing-provider -> ``None`` degradation, clear() cleanup, independence from
the prompt data provider, and the agent-side implementation's forwarding
contract: every method must resolve the real ``skill_manage`` primitive at
call time so monkeypatches of ``skill_manage`` keep intercepting the calls.
"""

from typing import Any

import pytest

from runtime import data_provider

pytestmark = [pytest.mark.unit]

_PROVIDER_METHODS = (
    "create_skill",
    "write_file",
    "split_oversized_skill",
    "umbrella_skill_char_target",
)


class _StubSkillWriter:
    """Minimal stand-in carrying the registry identity."""

    def create_skill(self, name: str, content: str) -> dict[str, Any]:
        return {"success": True, "name": name}


@pytest.fixture(autouse=True)
def _clean_registries():
    """Every test starts and ends with no registered provider."""
    data_provider.clear_prompt_data_provider()
    data_provider.clear_skill_write_provider()
    yield
    data_provider.clear_prompt_data_provider()
    data_provider.clear_skill_write_provider()


class TestMissingProvider:
    def test_unregistered_provider_is_none(self):
        assert data_provider.get_skill_write_provider() is None

    def test_clear_without_registration_is_noop(self):
        data_provider.clear_skill_write_provider()

        assert data_provider.get_skill_write_provider() is None


class TestRegisterResolve:
    def test_set_then_get_returns_same_object(self):
        stub = _StubSkillWriter()

        data_provider.set_skill_write_provider(stub)

        assert data_provider.get_skill_write_provider() is stub

    def test_re_registration_is_last_writer_wins(self):
        first = _StubSkillWriter()
        second = _StubSkillWriter()

        data_provider.set_skill_write_provider(first)
        data_provider.set_skill_write_provider(second)

        assert data_provider.get_skill_write_provider() is second

    def test_clear_drops_registration(self):
        data_provider.set_skill_write_provider(_StubSkillWriter())

        data_provider.clear_skill_write_provider()

        assert data_provider.get_skill_write_provider() is None

    def test_skill_writer_and_prompt_provider_are_independent(self):
        from agent.prompt_data_provider import AgentPromptDataProvider

        writer = _StubSkillWriter()
        data_provider.set_skill_write_provider(writer)
        data_provider.set_prompt_data_provider(AgentPromptDataProvider())

        data_provider.clear_skill_write_provider()

        assert data_provider.get_skill_write_provider() is None
        assert data_provider.get_prompt_data_provider() is not None


class TestAgentSkillWriterShape:
    def test_agent_writer_implements_every_protocol_method(self):
        from agent.skill_write_provider import AgentSkillWriteProvider

        writer = AgentSkillWriteProvider()

        missing = [name for name in _PROVIDER_METHODS if not callable(getattr(writer, name, None))]
        assert missing == []


class TestAgentSkillWriterForwarding:
    def test_create_skill_forwards_to_skill_manage(self, monkeypatch):
        from agent.skill_write_provider import AgentSkillWriteProvider
        from agent.tools.skill_tools import skill_manage

        calls: list[tuple[str, str]] = []

        def fake_create(name: str, content: str) -> dict[str, Any]:
            calls.append((name, content))
            return {"success": True, "marker": "created"}

        monkeypatch.setattr(skill_manage, "_create_skill", fake_create)

        result = AgentSkillWriteProvider().create_skill("s1", "body")

        assert result == {"success": True, "marker": "created"}
        assert calls == [("s1", "body")]

    def test_write_file_forwards_to_skill_manage(self, monkeypatch):
        from agent.skill_write_provider import AgentSkillWriteProvider
        from agent.tools.skill_tools import skill_manage

        calls: list[tuple[str, str, str]] = []

        def fake_write(name: str, file_path: str, file_content: str) -> dict[str, Any]:
            calls.append((name, file_path, file_content))
            return {"success": True, "marker": "written"}

        monkeypatch.setattr(skill_manage, "_write_file", fake_write)

        result = AgentSkillWriteProvider().write_file("s1", "references/a.md", "A")

        assert result == {"success": True, "marker": "written"}
        assert calls == [("s1", "references/a.md", "A")]

    def test_split_oversized_skill_forwards_to_skill_manage(self, monkeypatch):
        from agent.skill_write_provider import AgentSkillWriteProvider
        from agent.tools.skill_tools import skill_manage

        calls: list[tuple[str, int, dict[str, str] | None]] = []
        sentinel: tuple[str, dict[str, str]] = ("slim", {"references/part01.md": "P"})

        def fake_split(
            main_content: str, target: int, supporting_files: dict[str, str] | None = None
        ) -> tuple[str, dict[str, str]]:
            calls.append((main_content, target, supporting_files))
            return sentinel

        monkeypatch.setattr(skill_manage, "split_oversized_skill", fake_split)

        result = AgentSkillWriteProvider().split_oversized_skill("body", 123, {"a": "b"})

        assert result is sentinel
        assert calls == [("body", 123, {"a": "b"})]

    def test_umbrella_skill_char_target_reads_feature_config(self):
        from agent.skill_write_provider import AgentSkillWriteProvider
        from config.features import TOOLS_TIMEOUTS

        target = AgentSkillWriteProvider().umbrella_skill_char_target()

        assert target == TOOLS_TIMEOUTS["skill_manage_umbrella_skill_char_target"]
