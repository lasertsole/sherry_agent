"""Code Intel / LSP access by functional role.

The tool FACE is shared by the four CODE_INTEL_ROLES (researcher, librarian,
executor, general); reviewer stays out. The read-only/write difference is
prompt-only. These tests pin both halves plus the spawn allow-list invariant.
"""

from __future__ import annotations

import pytest

import agent.tools as agent_tools
from agent.tools.subagent.spawn.child_agent_factory import _assemble_child_tools
from agent.tools.subagent.spawn.system_prompt import build_subagent_system_prompt
from agent.tools.subagent.types import (
    CODE_INTEL_ROLES,
    CODE_INTEL_WRITE_ROLES,
    SPAWN_ALLOWED_CHILD_ROLES,
    FunctionalRole,
    SubagentSessionRole,
)

pytestmark = [pytest.mark.unit]

CODE_INTEL_NAMES = {"explore", "callers", "callees", "impact", "path"}
LSP_NAMES = {
    "lsp_goto_definition",
    "lsp_find_references",
    "lsp_workspace_symbol",
    "lsp_call_hierarchy",
    "lsp_rename",
    "lsp_diagnostics",
    "lsp_format",
    "lsp_status",
}


def _tool_names(role: FunctionalRole, monkeypatch) -> set[str]:
    monkeypatch.setattr(agent_tools, "build_main_tools", lambda: [])
    tools = _assemble_child_tools(None, None, None, None, role, "s1")
    return {tool.name for tool in tools}


@pytest.mark.parametrize("role", [FunctionalRole.EXECUTOR, FunctionalRole.GENERAL])
def test_write_roles_get_code_intel_and_lsp(role, monkeypatch):
    names = _tool_names(role, monkeypatch)

    assert CODE_INTEL_NAMES <= names
    assert LSP_NAMES <= names
    # ast-grep stays universal; only the executor also gets PTC.
    assert {"ast_grep_search", "ast_grep_rewrite"} <= names
    assert ("execute_code" in names) is (role is FunctionalRole.EXECUTOR)


@pytest.mark.parametrize("role", [FunctionalRole.RESEARCHER, FunctionalRole.LIBRARIAN])
def test_research_roles_are_unchanged(role, monkeypatch):
    names = _tool_names(role, monkeypatch)

    assert CODE_INTEL_NAMES <= names
    assert LSP_NAMES <= names
    assert "execute_code" not in names


def test_reviewer_still_has_no_code_intel(monkeypatch):
    names = _tool_names(FunctionalRole.REVIEWER, monkeypatch)

    assert not (CODE_INTEL_NAMES & names)
    assert not (LSP_NAMES & names)
    assert {"ast_grep_search", "ast_grep_rewrite"} <= names


def test_role_sets_and_spawn_allow_list_are_consistent():
    assert CODE_INTEL_ROLES == {
        FunctionalRole.RESEARCHER,
        FunctionalRole.LIBRARIAN,
        FunctionalRole.EXECUTOR,
        FunctionalRole.GENERAL,
    }
    assert CODE_INTEL_WRITE_ROLES == {FunctionalRole.EXECUTOR, FunctionalRole.GENERAL}
    # The single-hop containment rule is untouched: a write role may still only
    # reach write roles plus the two read-only research roles.
    assert SPAWN_ALLOWED_CHILD_ROLES[FunctionalRole.EXECUTOR] == {
        FunctionalRole.EXECUTOR,
        FunctionalRole.RESEARCHER,
        FunctionalRole.LIBRARIAN,
    }
    assert FunctionalRole.REVIEWER not in CODE_INTEL_ROLES


@pytest.mark.parametrize("role", [FunctionalRole.EXECUTOR, FunctionalRole.GENERAL])
def test_write_role_prompt_allows_applying_rename_and_format(role):
    prompt = build_subagent_system_prompt(
        SubagentSessionRole.LEAF,
        "Do X",
        functional_role=role,
        role_description="Write worker",
    )

    assert "You MAY pass dry_run=false" in prompt
    assert "You MAY pass write=true" in prompt
    assert "spawn a researcher for a heavy multi-step investigation" in prompt
    assert "Never pass dry_run=false" not in prompt
    assert "your role is read-only" not in prompt


@pytest.mark.parametrize("role", [FunctionalRole.RESEARCHER, FunctionalRole.LIBRARIAN])
def test_read_only_role_prompt_keeps_preview_mode(role):
    prompt = build_subagent_system_prompt(
        SubagentSessionRole.LEAF,
        "Do X",
        functional_role=role,
        role_description="Read-only research worker",
    )

    assert "Never pass dry_run=false" in prompt
    assert "Never pass write=true" in prompt
    assert "You MAY pass dry_run=false" not in prompt


@pytest.mark.parametrize("role", [FunctionalRole.EXECUTOR, FunctionalRole.GENERAL])
def test_write_role_spawn_note_does_not_claim_read_only(role):
    prompt = build_subagent_system_prompt(
        SubagentSessionRole.ORCHESTRATOR,
        "Do X",
        functional_role=role,
        role_description="Write worker",
        can_spawn=True,
    )

    assert "## Code Intelligence Tools" in prompt
    assert "you still do not edit files" not in prompt
