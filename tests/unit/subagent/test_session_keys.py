"""Unit tests for agent.tools.subagent.registry.session_keys.

Contract (frozen for downstream consumers):
    normalize_session_key(raw: str) -> str   — strip ``agent:main:session:`` to a bare id
    denormalize_session_key(bare: str) -> str — reassemble the prefixed key

Format authority: agent/tools/subagent/events/bridge.py::_strip_session_prefix
Prefix is matched case-insensitively for tolerance; the id portion is preserved verbatim.
"""

from agent.tools.subagent.registry.session_keys import (
    denormalize_session_key,
    normalize_session_key,
)

PREFIX = "agent:main:session:"


class TestNormalizeWithPrefix:
    def test_canonical_prefixed_key(self):
        assert normalize_session_key("agent:main:session:abc123") == "abc123"

    def test_uuid_like_id(self):
        key = f"{PREFIX}9f8e7d6c-1234-5678-9abc-def012345678"
        assert normalize_session_key(key) == "9f8e7d6c-1234-5678-9abc-def012345678"

    def test_only_prefix_returns_empty(self):
        assert normalize_session_key(PREFIX) == ""

    def test_id_case_is_preserved(self):
        assert normalize_session_key(f"{PREFIX}AbC-Id_42") == "AbC-Id_42"


class TestNormalizeBare:
    def test_bare_id_returned_as_is(self):
        assert normalize_session_key("bare-id") == "bare-id"

    def test_bare_id_with_colons_untouched(self):
        # Child session keys (agent:{id}:subagent:{uuid}) do NOT carry the
        # agent:main:session: prefix — tolerance means returning them unchanged.
        assert normalize_session_key("agent:main:subagent:abc") == "agent:main:subagent:abc"

    def test_nested_child_chain_untouched(self):
        key = "agent:main:subagent:abc:subagent:def"
        assert normalize_session_key(key) == key

    def test_swarm_key_untouched(self):
        key = "agent:main:swarm:g1:abc123"
        assert normalize_session_key(key) == key

    def test_plain_word_untouched(self):
        assert normalize_session_key("just-a-string") == "just-a-string"


class TestNormalizeEmptyAndNone:
    def test_empty_string(self):
        assert normalize_session_key("") == ""

    def test_none_returns_empty(self):
        assert normalize_session_key(None) == ""  # pyright: ignore[reportArgumentType]

    def test_whitespace_only_returns_empty(self):
        assert normalize_session_key("   ") == ""

    def test_whitespace_around_prefixed_key(self):
        assert normalize_session_key(f"  {PREFIX}abc123  ") == "abc123"

    def test_whitespace_around_bare_id(self):
        assert normalize_session_key("  bare-id  ") == "bare-id"


class TestNormalizeCaseTolerance:
    def test_uppercase_prefix(self):
        assert normalize_session_key("AGENT:MAIN:SESSION:abc123") == "abc123"

    def test_mixed_case_prefix(self):
        assert normalize_session_key("Agent:Main:Session:abc123") == "abc123"


class TestDenormalize:
    def test_bare_id_gets_prefix(self):
        assert denormalize_session_key("abc123") == "agent:main:session:abc123"

    def test_already_prefixed_not_double_prefixed(self):
        key = f"{PREFIX}abc123"
        assert denormalize_session_key(key) == key

    def test_uuid_like_id(self):
        bare = "9f8e7d6c-1234-5678-9abc-def012345678"
        assert denormalize_session_key(bare) == f"{PREFIX}{bare}"

    def test_empty_string(self):
        assert denormalize_session_key("") == ""

    def test_none_returns_empty(self):
        assert denormalize_session_key(None) == ""  # pyright: ignore[reportArgumentType]

    def test_whitespace_only_returns_empty(self):
        assert denormalize_session_key("   ") == ""

    def test_whitespace_is_trimmed(self):
        assert denormalize_session_key("  abc123  ") == f"{PREFIX}abc123"


class TestRoundtrip:
    def test_roundtrip_from_prefixed(self):
        original = f"{PREFIX}abc123"
        assert denormalize_session_key(normalize_session_key(original)) == original

    def test_roundtrip_from_bare(self):
        bare = "abc123"
        assert normalize_session_key(denormalize_session_key(bare)) == bare

    def test_roundtrip_uuid(self):
        bare = "9f8e7d6c-1234-5678-9abc-def012345678"
        assert normalize_session_key(denormalize_session_key(bare)) == bare


class TestBridgeParity:
    """normalize must be character-compatible with events/bridge.py::_strip_session_prefix
    for the string inputs that bridge handles (None/empty bridge maps to None-drop;
    here the frozen ``-> str`` contract maps them to "")."""

    def test_matches_bridge_for_prefixed(self):
        assert normalize_session_key(f"{PREFIX}abc") == "abc"

    def test_matches_bridge_for_unprefixed(self):
        assert normalize_session_key("xyz") == "xyz"
