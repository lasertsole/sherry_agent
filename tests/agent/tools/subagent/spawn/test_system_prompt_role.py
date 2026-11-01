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


class TestResearchRoleSpawning:
    """Non-leaf research roles get spawn guidance; leaves get neither section nor tool."""

    @pytest.mark.parametrize("role", [FunctionalRole.RESEARCHER, FunctionalRole.LIBRARIAN])
    def test_orchestrator_research_prompt_names_both_research_roles(self, role):
        prompt = build_subagent_system_prompt(
            SubagentSessionRole.ORCHESTRATOR,
            "Do X",
            functional_role=role,
            role_description="Read-only research worker",
            can_spawn=True,
        )

        assert "ORCHESTRATOR" in prompt
        assert "## Sub-Agent Spawning" in prompt
        assert "Allowed child roles" in prompt
        assert "researcher" in prompt
        assert "librarian" in prompt
        assert "you still do not edit files" in prompt
        assert "CANNOT spawn" not in prompt

    @pytest.mark.parametrize("role", [FunctionalRole.RESEARCHER, FunctionalRole.LIBRARIAN])
    def test_leaf_research_prompt_has_no_spawn_section(self, role):
        prompt = build_subagent_system_prompt(
            SubagentSessionRole.LEAF,
            "Do X",
            functional_role=role,
            role_description="Read-only research worker",
            can_spawn=False,
        )

        assert "CANNOT spawn" in prompt
        assert "## Sub-Agent Spawning" not in prompt
        assert "Allowed child roles" not in prompt

    def test_leaf_executor_role_body_does_not_claim_it_can_spawn(self):
        from agent.tools.subagent.roles.loader import load_role_definition

        definition = load_role_definition(FunctionalRole.EXECUTOR)
        prompt = build_subagent_system_prompt(
            SubagentSessionRole.LEAF,
            "Do X",
            functional_role=FunctionalRole.EXECUTOR,
            role_description=definition.description,
            role_prompt_body=definition.prompt_body,
            can_spawn=False,
        )

        assert "CANNOT spawn" in prompt
        assert "When your generated role section says you may spawn subagents" in prompt
        assert "If spawning is not allowed" in prompt
        assert "## Sub-Agent Spawning" not in prompt

    @pytest.mark.parametrize("role", [FunctionalRole.RESEARCHER, FunctionalRole.LIBRARIAN])
    def test_spawn_tool_is_added_only_for_an_orchestrator(self, role):
        from agent.tools.subagent.roles.loader import load_role_definition
        from agent.tools.subagent.spawn.core import _resolve_child_tool_policy

        definition = load_role_definition(role)
        orchestrator_allow, _ = _resolve_child_tool_policy(
            definition, SubagentSessionRole.ORCHESTRATOR, []
        )
        leaf_allow, _ = _resolve_child_tool_policy(definition, SubagentSessionRole.LEAF, [])

        control_tools = {
            "sessions_spawn",
            "sessions_yield",
            "subagents_list",
            "sessions_send",
        }
        assert control_tools <= set(orchestrator_allow)
        assert not control_tools & set(leaf_allow)
        # main_only management tools can never reach a child.
        assert "sessions_kill" not in orchestrator_allow
        assert "sessions_steer" not in orchestrator_allow
