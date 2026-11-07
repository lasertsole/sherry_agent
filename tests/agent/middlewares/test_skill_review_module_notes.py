"""Skill review: module-notes guidance + memory-review FACTS routing.

The plan-completion pass is now skill-only. It no longer writes knowledge or
FACTS; module-bound lessons still go to ``<module>-notes`` skills. The memory
review remains the FACTS writer and keeps its system-level routing tests.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

import agent.middlewares.summarization.background_reviews as review_mod
import agent.middlewares.summarization.skill_review as skill_review
import agent.tools.skill_tools.skill_manage as skill_manage_module
from agent.tools.skill_tools.skill_manage import build_skill_manage_tool

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]


class _FakeStateRegister:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], object] = {}

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self.data.get((session_id, key), default)

    def set_state(self, session_id: str, key: str, value: object) -> None:
        self.data[(session_id, key)] = value


class _FakeFactsStore:
    facts_char_limit = 1_375

    def __init__(self, content: str = "KNOWN-FACT: test_auth_17 needs network") -> None:
        self.content = content

    def format_live_content(self, target: str, session_id: str | None = None) -> str:
        assert target == "facts"
        return self.content


class _PassingJudgement:
    passed = True
    verdict = "pass"
    grounded_in_run = True
    reusable = True
    trigger_quality = 8
    quality = 8
    no_op_justified = False
    feedback = ""


@pytest.fixture(autouse=True)
def _passing_skill_judge(monkeypatch: pytest.MonkeyPatch):
    async def _pass(*_args, **_kwargs):
        return _PassingJudgement()

    monkeypatch.setattr(review_mod, "judge_skill_update", _pass)


class TestSkillPrompt:
    def test_module_notes_carry_forward_is_still_present(self):
        prompt = review_mod._SKILL_REVIEW_PROMPT

        assert "<module>-notes" in prompt
        assert "auth-module-notes" in prompt
        assert "symptom" in prompt and "avoidance" in prompt

    def test_prompt_is_skill_only(self):
        prompt = review_mod._SKILL_REVIEW_PROMPT

        assert "Do NOT write knowledge" in prompt
        assert "aggregate_success_steps" not in prompt
        assert "Part 1: Structured Knowledge Extraction" not in prompt
        assert "Part 3" not in prompt
        assert 'target="facts"' not in prompt
        assert "feedback.loaded_skills" in prompt
        assert "feedback.user_corrections" in prompt


class TestMemoryReviewFacts:
    def test_memory_review_prompt_has_facts_routing(self):
        prompt = review_mod._MEMORY_REVIEW_PROMPT

        assert "target: 'facts'" in prompt
        assert "{facts_limit}" in prompt
        assert "{facts_block}" in prompt
        assert "skill" not in prompt.lower()

    def test_facts_rendering_injects_live_content_and_limit(self, monkeypatch):
        monkeypatch.setattr("agent.tools.memory.memory_store", _FakeFactsStore())

        rendered = review_mod._render_prompt_facts(
            "limit={facts_limit}\n<facts>\n{facts_block}\n</facts>"
        )

        assert "1,375" in rendered
        assert "KNOWN-FACT: test_auth_17 needs network" in rendered
        assert "{facts_block}" not in rendered
        assert "{facts_limit}" not in rendered

    def test_facts_rendering_marks_empty_store(self, monkeypatch):
        monkeypatch.setattr("agent.tools.memory.memory_store", _FakeFactsStore(content=""))

        assert "(the facts target is empty)" in review_mod._render_prompt_facts("{facts_block}")


_CREATE_CONTENT = """---
name: auth-module-notes
description: Module-bound notes for the auth module.
---

## auth module

- test_auth_17 requires network → fails offline → run with network up
"""

_SEED_CONTENT = """---
name: auth-module-notes
description: Module-bound notes for the auth module.
---

## auth module

- existing row
"""


def _stub_model(tool_call: dict) -> BaseChatModel:
    class _StubModuleNotesModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "stub-module-notes"

        def bind_tools(self, tools, **kwargs):  # noqa: ARG002
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ARG002
            if any(isinstance(m, ToolMessage) for m in messages):
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="done"))])
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[tool_call]))]
            )

    return _StubModuleNotesModel()


def _install_stub(monkeypatch, tmp_path, tool_call: dict) -> SimpleNamespace:
    auto_dir = tmp_path / "skills" / "auto"
    auto_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(skill_manage_module, "AUTO_SKILLS_DIR", auto_dir)
    monkeypatch.setattr(skill_review, "AUTO_SKILLS_DIR", auto_dir)
    monkeypatch.setattr("skills.build_skills_snapshot", lambda: None)

    import agent.tools.pub_base as pub_base

    for name in ("bump_patch", "forget", "mark_agent_created"):
        monkeypatch.setattr(pub_base, name, lambda *a, **k: None)
    monkeypatch.setattr(pub_base, "is_background_review", lambda: False)

    monkeypatch.setattr("models.build_auxiliary_llm", lambda **kwargs: _stub_model(tool_call))
    monkeypatch.setattr(
        review_mod, "_build_session_model", lambda session_id: _stub_model(tool_call)
    )
    monkeypatch.setattr("agent.get_agent_tools", lambda: [build_skill_manage_tool()])
    monkeypatch.setattr(review_mod, "state_register_mem", _FakeStateRegister())
    monkeypatch.setattr(review_mod, "state_register_db", _FakeStateRegister())
    monkeypatch.setattr(
        "agent.middlewares.system_prompt.core._get_and_reload_system_prompt",
        lambda session_id: "sys",
    )
    monkeypatch.setattr(
        review_mod,
        "_build_plan_context",
        lambda sid: {"plan_name": "p", "plan_path": "x", "plan_content": "", "todos": []},
    )
    monkeypatch.setattr("agent.tools.memory.memory_store", _FakeFactsStore())

    return SimpleNamespace(auto_dir=auto_dir)


class TestModuleNotesBehavior:
    @pytest.mark.asyncio
    async def test_fake_llm_creates_module_notes_skill(self, monkeypatch, tmp_path):
        env = _install_stub(
            monkeypatch,
            tmp_path,
            {
                "name": "skill_manage",
                "args": {
                    "action": "create",
                    "name": "auth-module-notes",
                    "content": _CREATE_CONTENT,
                },
                "id": "call-notes-create",
                "type": "tool_call",
            },
        )

        await review_mod._run_skill_review("sess-notes-create")

        skill_md = env.auto_dir / "auth-module-notes" / "SKILL.md"
        assert skill_md.exists()
        assert "test_auth_17" in skill_md.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_fake_llm_updates_existing_module_notes_without_duplicating(
        self, monkeypatch, tmp_path
    ):
        existing_dir = tmp_path / "skills" / "auto" / "auth-module-notes"
        existing_dir.mkdir(parents=True)
        existing_md = existing_dir / "SKILL.md"
        existing_md.write_text(_SEED_CONTENT, encoding="utf-8")

        env = _install_stub(
            monkeypatch,
            tmp_path,
            {
                "name": "skill_manage",
                "args": {
                    "action": "patch",
                    "name": "auth-module-notes",
                    "old_string": "- existing row",
                    "new_string": "- existing row\n- appended row",
                },
                "id": "call-notes-patch",
                "type": "tool_call",
            },
        )

        await review_mod._run_skill_review("sess-notes-patch")

        skill_dirs = sorted(p.name for p in env.auto_dir.iterdir() if p.is_dir())
        assert skill_dirs == ["auth-module-notes"]
        text = existing_md.read_text(encoding="utf-8")
        assert "- existing row" in text
        assert "- appended row" in text
