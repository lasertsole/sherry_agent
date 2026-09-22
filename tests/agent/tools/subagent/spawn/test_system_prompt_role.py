"""T1.4: role-specific system prompt sections."""

import pytest

from agent.tools.subagent.spawn.system_prompt import build_subagent_system_prompt
from agent.tools.subagent.types import FunctionalRole, SubagentSessionRole

pytestmark = [pytest.mark.unit]


class TestRoleSpecificPrompt:
    def test_leaf_researcher_declares_specialization(self):
        prompt = build_subagent_system_prompt(
            SubagentSessionRole.LEAF,
            "Do X",
            functional_role=FunctionalRole.RESEARCHER,
            role_description="Read-only research worker",
        )
        assert "RESEARCHER specialization" in prompt
        assert "CANNOT spawn" in prompt

    def test_orchestrator_general_declares_both(self):
        prompt = build_subagent_system_prompt(
            SubagentSessionRole.ORCHESTRATOR,
            "Do X",
            functional_role=FunctionalRole.GENERAL,
            role_description="General-purpose worker",
        )
        assert "ORCHESTRATOR" in prompt
        assert "GENERAL" in prompt

    def test_role_prompt_body_injected_under_role_instructions(self):
        prompt = build_subagent_system_prompt(
            SubagentSessionRole.LEAF,
            "Do X",
            functional_role=FunctionalRole.EXECUTOR,
            role_description="Code execution worker",
            role_prompt_body="EXECUTOR-BODY-MARKER",
        )
        assert "## Role Instructions" in prompt
        assert "EXECUTOR-BODY-MARKER" in prompt

    def test_default_prompt_has_no_functional_role_decoration(self):
        prompt = build_subagent_system_prompt(SubagentSessionRole.LEAF, "Do X")
        assert "specialization" not in prompt
        assert "## Role Instructions" not in prompt
