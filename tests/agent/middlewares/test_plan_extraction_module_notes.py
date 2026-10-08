"""Tests for the plan-extraction module-notes carry-forward and FACTS routing.

Part 2 of ``_PLAN_EXTRACTION_PROMPT`` carries module-bound lessons into
``<module>-notes`` skills; Part 3 routes broad, module-independent pitfalls to
the ``facts`` memory target. The suite covers:

- prompt content: the carry-forward guidance, the ``<module>-notes`` naming
  convention, the create/patch branches, and the Part 3 FACTS instruction;
- rendering: the live FACTS.md content and char limit reach both nudge prompts;
- behavior: a stub LLM emitting the guidance-prescribed ``skill_manage`` calls —
  a new ``auth-module-notes`` skill lands under ``skills/auto/``, and an
  existing one is patched in place instead of duplicated.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

import agent.middlewares.summarization.nudges as nudge_mod
import agent.tools.skill_tools.skill_manage as skill_manage_module
from agent.tools.skill_tools.skill_manage import build_skill_manage_tool

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]


class _FakeStateRegister:
    """Minimal in-memory stand-in for ``state_register_mem`` (locks only)."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], object] = {}

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self.data.get((session_id, key), default)

    def set_state(self, session_id: str, key: str, value: object) -> None:
        self.data[(session_id, key)] = value


class _FakeFactsStore:
    """Stand-in for ``agent.tools.memory.memory_store`` (facts reads)."""

    facts_char_limit = 1_375

    def __init__(self, content: str = "KNOWN-FACT: test_auth_17 needs network") -> None:
        self.content = content

    def format_live_content(self, target: str) -> str:
        assert target == "facts"
        return self.content


# ---------------------------------------------------------------------------
# Prompt content
# ---------------------------------------------------------------------------


class TestPromptGuidance:
    def test_plan_extraction_prompt_has_module_notes_carry_forward(self):
        prompt = nudge_mod._PLAN_EXTRACTION_PROMPT

        assert "<module>-notes" in prompt
        assert 'action="create"' in prompt
        assert 'action="patch"' in prompt
        assert "auth-module-notes" in prompt
        assert "symptom" in prompt and "avoidance action" in prompt
        # Part 1 vs carry-forward split is spelled out.
        assert "Part 1 knowledge" in prompt

    def test_plan_extraction_prompt_has_facts_part(self):
        prompt = nudge_mod._PLAN_EXTRACTION_PROMPT

        assert "Do THREE things:" in prompt
        assert "## Part 3" in prompt
        assert 'target="facts"' in prompt
        assert "{facts_limit}" in prompt
        assert "{facts_block}" in prompt

    def test_memory_review_prompt_has_facts_routing(self):
        prompt = nudge_mod._MEMORY_REVIEW_PROMPT

        assert "target: 'facts'" in prompt
        assert "{facts_limit}" in prompt
        assert "{facts_block}" in prompt
        assert "'<module>-notes' skill" in prompt


# ---------------------------------------------------------------------------
# Rendering (live FACTS content + limit)
# ---------------------------------------------------------------------------


class TestFactsRendering:
    def test_render_prompt_facts_injects_live_content_and_limit(self, monkeypatch):
        monkeypatch.setattr("agent.tools.memory.memory_store", _FakeFactsStore())

        rendered = nudge_mod._render_prompt_facts(
            "limit={facts_limit}\n<facts>\n{facts_block}\n</facts>"
        )

        assert "1,375" in rendered
        assert "KNOWN-FACT: test_auth_17 needs network" in rendered
        assert "{facts_block}" not in rendered
        assert "{facts_limit}" not in rendered

    def test_render_prompt_facts_marks_empty_store(self, monkeypatch):
        monkeypatch.setattr("agent.tools.memory.memory_store", _FakeFactsStore(content=""))

        rendered = nudge_mod._render_prompt_facts("{facts_block}")

        assert "(FACTS.md is empty)" in rendered

    @pytest.mark.asyncio
    async def test_nudge_memory_prompt_carries_facts_context(self, monkeypatch):
        monkeypatch.setattr("agent.tools.memory.memory_store", _FakeFactsStore())
        prompts: list[str] = []

        class _CapturingAgent:
            async def ainvoke(self, input, **kwargs):  # noqa: ARG002
                prompts.append(input["messages"][-1].content)
                return {"messages": [AIMessage(content="nothing")]}

        async def _create(system_prompt, allowed_metadata_key=None, tools=None):
            return _CapturingAgent()

        monkeypatch.setattr(nudge_mod, "_create_nudge_agent", _create)

        await nudge_mod._nudge_memory("sess-review", "sys", [HumanMessage("hi")])

        assert len(prompts) == 1
        assert "KNOWN-FACT: test_auth_17 needs network" in prompts[0]
        assert "1,375" in prompts[0]
        assert "(FACTS.md is empty)" not in prompts[0]

    @pytest.mark.asyncio
    async def test_nudge_plan_extraction_prompt_carries_facts_context(self, monkeypatch):
        monkeypatch.setattr("agent.tools.memory.memory_store", _FakeFactsStore())
        monkeypatch.setattr(nudge_mod, "state_register_mem", _FakeStateRegister())
        monkeypatch.setattr(
            nudge_mod,
            "_build_plan_context",
            lambda sid: {"plan_name": "p", "plan_path": "x", "plan_content": "", "todos": []},
        )
        prompts: list[str] = []

        class _CapturingAgent:
            async def ainvoke(self, input, **kwargs):  # noqa: ARG002
                prompts.append(input["messages"][-1].content)
                return {"messages": [AIMessage(content="done")]}

        async def _create(system_prompt, allowed_metadata_key=None, tools=None):
            return _CapturingAgent()

        monkeypatch.setattr(nudge_mod, "_create_nudge_agent", _create)

        await nudge_mod._nudge_plan_extraction("sess-plan-facts", "sys", [HumanMessage("hi")])

        assert len(prompts) == 1
        assert "KNOWN-FACT: test_auth_17 needs network" in prompts[0]
        assert "{facts_block}" not in prompts[0]
        assert "<module>-notes" in prompts[0]


# ---------------------------------------------------------------------------
# Behavior: stub LLM emits the guidance-prescribed skill_manage calls
# ---------------------------------------------------------------------------

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
    """Wire the real nudge fork to a stub LLM + isolated auto skills dir."""
    auto_dir = tmp_path / "skills" / "auto"
    auto_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(skill_manage_module, "AUTO_SKILLS_DIR", auto_dir)
    monkeypatch.setattr("skills.build_skills_snapshot", lambda: None)

    import agent.tools.pub_base as pub_base

    for name in ("bump_patch", "forget", "mark_agent_created"):
        monkeypatch.setattr(pub_base, name, lambda *a, **k: None)
    monkeypatch.setattr(pub_base, "is_background_review", lambda: False)

    monkeypatch.setattr("models.build_main_llm", lambda: _stub_model(tool_call))
    monkeypatch.setattr("agent.get_agent_tools", lambda: [build_skill_manage_tool()])
    monkeypatch.setattr(nudge_mod, "state_register_mem", _FakeStateRegister())
    monkeypatch.setattr(
        nudge_mod,
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

        await nudge_mod._nudge_plan_extraction("sess-notes-create", "sys", [HumanMessage("hi")])

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

        await nudge_mod._nudge_plan_extraction("sess-notes-patch", "sys", [HumanMessage("hi")])

        skill_dirs = sorted(p.name for p in env.auto_dir.iterdir() if p.is_dir())
        assert skill_dirs == ["auth-module-notes"]
        text = existing_md.read_text(encoding="utf-8")
        assert "- existing row" in text
        assert "- appended row" in text
