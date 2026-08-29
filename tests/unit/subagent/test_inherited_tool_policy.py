"""Unit tests for metadata-based tool visibility in the inherited tool policy.

Covers the shared contract:
  - ``metadata["scope"] == "main_only"`` tools are non-overridably denied to
    subagents (before allow/deny logic).
  - Every returned tool is stamped ``metadata["caller_scope"] = "subagent"``.
  - ``DEFAULT_SUBAGENT_BLOCKED_TOOLS`` is slimmed to spawn/yield only, and the
    fallback fires only when ``tool_deny is None`` (not for an explicit empty
    list, which is what ORCHESTRATOR produces after unblocking spawn/yield).
"""

from types import SimpleNamespace

from agent.tools.subagent.spawn.inherited_tool_policy import (
    apply_tool_policy,
    DEFAULT_SUBAGENT_BLOCKED_TOOLS,
)


def _tool(name: str, metadata: dict | None = None, with_metadata: bool = True):
    """Build a dummy tool matching the FakeTool fixtures used in test_spawn.py.

    ``with_metadata=False`` builds a tool that lacks the metadata attribute
    entirely (to exercise defensive handling).
    """
    t = SimpleNamespace(name=name)
    if with_metadata:
        t.metadata = dict(metadata) if metadata else {}
    return t


class TestSlimmedDefaultDenylist:
    def test_default_blocked_tools_slimmed_to_spawn_yield(self):
        """The name-based blacklist contains ONLY the conditionally-unblockable
        spawn/yield tools; the other four moved to metadata scope tags."""
        assert DEFAULT_SUBAGENT_BLOCKED_TOOLS == ["sessions_spawn", "sessions_yield"]

    def test_four_high_risk_tools_not_in_blacklist(self):
        for name in ("memory", "skill_manage", "sessions_kill", "sessions_steer"):
            assert name not in DEFAULT_SUBAGENT_BLOCKED_TOOLS


class TestNoneVsEmptyDenySemantics:
    def test_none_deny_triggers_fallback(self):
        """tool_deny=None means "no policy provided" → fallback applies."""
        tools = [_tool("read"), _tool("sessions_spawn"), _tool("sessions_yield")]
        result = apply_tool_policy(tools, [], None)
        names = [t.name for t in result]
        assert "sessions_spawn" not in names
        assert "sessions_yield" not in names
        assert "read" in names

    def test_explicit_empty_deny_is_authoritative(self):
        """tool_deny=[] means the caller intentionally emptied the deny-list
        (ORCHESTRATOR unblock) — the fallback must NOT re-add spawn/yield."""
        tools = [_tool("read"), _tool("sessions_spawn"), _tool("sessions_yield")]
        result = apply_tool_policy(tools, [], [])
        names = [t.name for t in result]
        assert "sessions_spawn" in names
        assert "sessions_yield" in names
        assert "read" in names


class TestMainOnlyScope:
    def test_main_only_denied_with_empty_deny(self):
        """A main_only tool is dropped even with an explicitly empty deny-list."""
        tools = [_tool("memory", {"scope": "main_only"}), _tool("read")]
        result = apply_tool_policy(tools, [], [])
        assert [t.name for t in result] == ["read"]

    def test_main_only_overrides_allow_list(self):
        """Allow-listing a main_only tool does not re-grant it (non-overridable)."""
        tools = [_tool("memory", {"scope": "main_only"})]
        result = apply_tool_policy(tools, ["memory"], [])
        assert result == []

    def test_main_only_denied_for_leaf_default_deny(self):
        """LEAF-style deny (slimmed DEFAULT + name deny): main_only still dropped,
        spawn/yield denied by name, regular tools kept."""
        tools = [
            _tool("read"),
            _tool("sessions_spawn"),
            _tool("memory", {"scope": "main_only"}),
            _tool("sessions_kill", {"scope": "main_only"}),
        ]
        result = apply_tool_policy(tools, [], list(DEFAULT_SUBAGENT_BLOCKED_TOOLS))
        names = [t.name for t in result]
        assert names == ["read"]

    def test_main_only_precedence_over_orchestrator_unblock(self):
        """ORCHESTRATOR unblock (empty deny + explicit allow) cannot re-grant
        main_only tools, while spawn/yield stay available for recursion."""
        tools = [
            _tool("sessions_spawn"),
            _tool("sessions_yield"),
            _tool("sessions_kill", {"scope": "main_only"}),
            _tool("sessions_steer", {"scope": "main_only"}),
        ]
        result = apply_tool_policy(
            tools,
            ["sessions_spawn", "sessions_yield", "sessions_kill", "sessions_steer"],
            [],
        )
        names = [t.name for t in result]
        assert "sessions_spawn" in names
        assert "sessions_yield" in names
        assert "sessions_kill" not in names
        assert "sessions_steer" not in names


class TestCallerScopeStamping:
    def test_returned_tools_stamped_subagent(self):
        tools = [_tool("read"), _tool("write", {"idempotent": True})]
        result = apply_tool_policy(tools, [], None)
        for t in result:
            assert t.metadata["caller_scope"] == "subagent"

    def test_stamping_preserves_existing_metadata(self):
        t = _tool("write", {"idempotent": True})
        result = apply_tool_policy([t], [], None)
        assert len(result) == 1
        assert result[0].metadata == {"idempotent": True, "caller_scope": "subagent"}

    def test_filtered_out_tools_not_stamped(self):
        denied = _tool("sessions_spawn")
        metadata_blocked = _tool("memory", {"scope": "main_only"})
        apply_tool_policy([denied, metadata_blocked], [], None)
        assert denied.metadata == {}
        assert metadata_blocked.metadata == {"scope": "main_only"}

    def test_stamp_is_idempotent(self):
        t = _tool("read", {"caller_scope": "subagent"})
        result = apply_tool_policy([t], [], None)
        assert result[0].metadata == {"caller_scope": "subagent"}


class TestDefensiveMetadataHandling:
    def test_tool_without_metadata_attr_does_not_crash(self):
        """A tool lacking the metadata attribute is kept and gets a fresh dict."""
        t = _tool("read", with_metadata=False)
        result = apply_tool_policy([t], [], None)
        assert [x.name for x in result] == ["read"]
        assert t.metadata == {"caller_scope": "subagent"}

    def test_tool_with_non_dict_metadata_does_not_crash(self):
        """A tool whose metadata is not a dict is kept; the filter must not raise."""
        t = _tool("read")
        t.metadata = "not-a-dict"
        result = apply_tool_policy([t], [], None)
        assert [x.name for x in result] == ["read"]

    def test_tool_with_none_metadata_does_not_crash(self):
        """langchain_core BaseTool.metadata defaults to None — must be handled."""
        t = _tool("read")
        t.metadata = None
        result = apply_tool_policy([t], [], None)
        assert [x.name for x in result] == ["read"]
        assert t.metadata == {"caller_scope": "subagent"}
