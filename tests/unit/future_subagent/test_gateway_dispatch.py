import pytest
from future_subagent.spawn.gateway_dispatch import resolve_least_privilege_scopes
from future_subagent.types.capability import SubagentSessionRole


class TestResolveLeastPrivilegeScopes:
    def test_orchestrator_scopes(self):
        scopes = resolve_least_privilege_scopes("main", SubagentSessionRole.ORCHESTRATOR)
        assert "subagent:read" in scopes
        assert "subagent:spawn" in scopes
        assert "subagent:kill" in scopes
        assert "subagent:yield" in scopes
        assert "subagent:send" in scopes

    def test_leaf_scopes(self):
        scopes = resolve_least_privilege_scopes("main", SubagentSessionRole.LEAF)
        assert "subagent:read" in scopes
        assert "subagent:yield" in scopes
        assert "subagent:spawn" not in scopes
        assert "subagent:kill" not in scopes

    def test_main_scopes(self):
        scopes = resolve_least_privilege_scopes("main", SubagentSessionRole.MAIN)
        assert "subagent:read" in scopes
        assert len(scopes) == 1

    def test_always_has_read(self):
        for role in SubagentSessionRole:
            scopes = resolve_least_privilege_scopes("main", role)
            assert "subagent:read" in scopes
