import pytest
from future_subagent.spawn.origin_routing import (
    ChildSessionOrigin,
    resolve_requester_origin_for_child,
)


class TestChildSessionOrigin:
    def test_defaults(self):
        origin = ChildSessionOrigin()
        assert origin.channel is None
        assert origin.account_id is None
        assert origin.thread_id is None
        assert origin.group_space is None
        assert origin.member_role_ids == []

    def test_custom(self):
        origin = ChildSessionOrigin(
            channel="ch1",
            account_id="acc1",
            thread_id="thr1",
            group_space="grp1",
        )
        assert origin.channel == "ch1"
        assert origin.account_id == "acc1"


class TestResolveRequesterOriginForChild:
    def test_standard_session_key(self):
        origin = resolve_requester_origin_for_child("agent:main:session:abc")
        assert origin.account_id == "session"
        assert origin.channel == "agent"

    def test_short_key(self):
        origin = resolve_requester_origin_for_child("short")
        assert origin.account_id is None
        assert origin.channel is None

    def test_subagent_key(self):
        origin = resolve_requester_origin_for_child("agent:main:subagent:child1")
        assert origin.account_id == "subagent"
        assert origin.channel == "agent"

    def test_with_agent_id(self):
        origin = resolve_requester_origin_for_child("agent:main:session:p1", agent_id="helper")
        assert origin.channel == "agent"
