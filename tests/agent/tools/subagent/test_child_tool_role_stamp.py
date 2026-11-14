"""The child tool face carries the functional role into consumer tools.

``_assemble_child_tools`` must pass a SPECIALIZED role to the tool policy (so
skill ``roles: [...]`` filters apply) and pass None for the identity GENERAL
role (the default delegation path keeps the pre-role full view).
"""

from __future__ import annotations

import pytest

import agent.tools.subagent.spawn.child_agent_factory as caf
from agent.tools.subagent.types.functional_role import FunctionalRole

pytestmark = [pytest.mark.unit]


@pytest.fixture
def policy_capture(monkeypatch):
    """Stub the tool policy + the role-gated tool families; capture the role."""
    captured: dict[str, object] = {}

    def _policy(tools, allow, deny, blocked_tools=None, *, caller_role=None):
        captured["role"] = caller_role
        return []

    monkeypatch.setattr(caf, "apply_tool_policy", _policy)
    monkeypatch.setattr("agent.tools.build_main_tools", lambda: [])
    monkeypatch.setattr(
        "agent.tools.code_intel.ast_grep.build_ast_grep_tools", lambda session_id: []
    )
    monkeypatch.setattr("agent.tools.code_intel.build_code_intel_tools", lambda session_id: [])
    monkeypatch.setattr("agent.tools.code_intel.lsp.build_lsp_tools", lambda session_id: [])
    return captured


def test_a_specialized_role_is_passed_to_the_policy(policy_capture):
    caf._assemble_child_tools(None, None, None, None, FunctionalRole.REVIEWER, "sess")

    assert policy_capture["role"] == "reviewer"


def test_the_identity_role_passes_no_role(policy_capture):
    caf._assemble_child_tools(None, None, None, None, FunctionalRole.GENERAL, "sess")

    assert policy_capture["role"] is None
