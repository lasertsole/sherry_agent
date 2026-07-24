import pytest
from future_subagent.capabilities.core import (
    resolve_subagent_capabilities,
    is_subagent_session,
    can_spawn_children,
    extract_depth_from_session_key,
)
from future_subagent.types.capability import SubagentSessionRole, ControlScope


class TestResolveCapabilities:
    def test_depth_0_is_main(self):
        role, scope = resolve_subagent_capabilities(0, 3)
        assert role == SubagentSessionRole.MAIN
        assert scope == ControlScope.CHILDREN

    def test_depth_1_is_orchestrator(self):
        role, scope = resolve_subagent_capabilities(1, 3)
        assert role == SubagentSessionRole.ORCHESTRATOR
        assert scope == ControlScope.CHILDREN

    def test_depth_at_max_is_leaf(self):
        role, scope = resolve_subagent_capabilities(3, 3)
        assert role == SubagentSessionRole.LEAF
        assert scope == ControlScope.NONE

    def test_depth_beyond_max_is_leaf(self):
        role, scope = resolve_subagent_capabilities(5, 3)
        assert role == SubagentSessionRole.LEAF
        assert scope == ControlScope.NONE

    def test_depth_1_max_1_is_leaf(self):
        role, scope = resolve_subagent_capabilities(1, 1)
        assert role == SubagentSessionRole.LEAF
        assert scope == ControlScope.NONE


class TestIsSubagentSession:
    def test_subagent_session(self):
        assert is_subagent_session("agent:main:subagent:abc123")

    def test_main_session(self):
        assert not is_subagent_session("agent:main:session:xyz")

    def test_plain_string(self):
        assert not is_subagent_session("just-a-string")


class TestCanSpawnChildren:
    def test_main_can_spawn(self):
        assert can_spawn_children(SubagentSessionRole.MAIN)

    def test_orchestrator_can_spawn(self):
        assert can_spawn_children(SubagentSessionRole.ORCHESTRATOR)

    def test_leaf_cannot_spawn(self):
        assert not can_spawn_children(SubagentSessionRole.LEAF)


class TestExtractDepth:
    def test_depth_0(self):
        assert extract_depth_from_session_key("agent:main:session:abc") == 0

    def test_depth_1(self):
        assert extract_depth_from_session_key("agent:main:subagent:abc") == 1

    def test_nested_depth(self):
        key = "agent:main:subagent:abc:subagent:def"
        assert extract_depth_from_session_key(key) == 2
