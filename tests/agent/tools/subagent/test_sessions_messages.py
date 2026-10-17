"""sessions_messages: reading a subagent's checkpoint transcript after a timeout.

Covers the permission gate (only the spawner / the run's completion owner may
read), the limit clamp, the token-budgeted rendering, and — the premise the
whole feature rests on — that a CHILD's checkpoint written through one
connection is readable through another after the child released its own.
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.tools.subagent.tools import sessions_messages as sm

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_CALLER = "agent:main:session:owner"
_CHILD = "agent:main:subagent:child-1"


class _StubRun:
    def __init__(self, spawned_by: str | None, requester: str = "agent:main:session:owner"):
        self.spawned_by = spawned_by
        self.requester_session_key = requester


@pytest.fixture()
def stub_run(monkeypatch):
    """Resolve the child's run record from a stub (no registry writes)."""
    import agent.tools.subagent.registry as registry

    holder: dict[str, _StubRun | None] = {"run": None}
    monkeypatch.setattr(registry, "get_run_by_child_session_key", lambda key: holder["run"])
    return holder


class TestPermissionGate:
    def test_the_spawner_may_read(self, stub_run):
        stub_run["run"] = _StubRun(spawned_by=_CALLER)

        allowed, reason = sm.check_read_permission("owner", _CHILD)

        assert allowed, reason

    def test_the_completion_owner_may_read_a_proxied_spawn(self, stub_run):
        stub_run["run"] = _StubRun(spawned_by=None, requester="agent:main:session:owner")

        allowed, _ = sm.check_read_permission("owner", _CHILD)

        assert allowed

    def test_an_unrelated_session_is_denied(self, stub_run):
        stub_run["run"] = _StubRun(spawned_by=_CALLER)

        allowed, reason = sm.check_read_permission("someone-else", _CHILD)

        assert not allowed
        assert "not the spawning session" in reason

    def test_an_unknown_target_is_denied(self, stub_run):
        stub_run["run"] = None

        allowed, reason = sm.check_read_permission("owner", _CHILD)

        assert not allowed
        assert "no subagent run found" in reason

    def test_a_blank_caller_is_denied(self, stub_run):
        stub_run["run"] = _StubRun(spawned_by=_CALLER)

        allowed, reason = sm.check_read_permission("   ", _CHILD)

        assert not allowed
        assert "unknown calling session" in reason

    def test_a_session_may_read_its_own_transcript(self, stub_run):
        stub_run["run"] = None

        allowed, _ = sm.check_read_permission("owner", "agent:main:session:owner")

        assert allowed


class TestLimitClamp:
    def test_bounds_and_default(self):
        assert sm.clamp_limit(0) == sm.MIN_LIMIT
        assert sm.clamp_limit(-5) == sm.MIN_LIMIT
        assert sm.clamp_limit(99) == sm.MAX_LIMIT
        assert sm.clamp_limit(30) == 30
        assert sm.clamp_limit("7") == 7

    def test_a_non_numeric_limit_degrades_to_the_default(self):
        assert sm.clamp_limit(None) == sm.DEFAULT_LIMIT
        assert sm.clamp_limit("many") == sm.DEFAULT_LIMIT


class TestRendering:
    def test_tool_calls_and_their_arguments_are_rendered(self):
        message = AIMessage(
            content="let me read the file",
            tool_calls=[
                {"name": "read_file", "args": {"path": "src/foo.py"}, "id": "c1"},
                {"name": "terminal", "args": {"command": "pytest -q"}, "id": "c2"},
            ],
        )

        text = sm.message_text(message)

        assert "let me read the file" in text
        assert "read_file(path=src/foo.py)" in text
        assert "terminal(command=pytest -q)" in text

    def test_a_tool_result_message_renders_its_text(self):
        text = sm.message_text(ToolMessage(content="3 passed", tool_call_id="c1"))

        assert "3 passed" in text

    def test_the_transcript_is_token_budgeted(self):
        from pub.func.message.token_truncation import estimate_text_tokens

        messages = [AIMessage(content="x" * 4000) for _ in range(20)]

        rendered = sm.render_messages(messages, _CHILD)

        # The shared truncator appends its omission marker ON TOP of the
        # head/tail budget, so the result may overshoot by the marker's width.
        assert estimate_text_tokens(rendered) <= 4000 + 64
        assert rendered.startswith(f"## Subagent session: {_CHILD}")
        assert "[ai]" in rendered


class TestTranscriptTool:
    @pytest.mark.asyncio
    async def test_an_empty_checkpoint_reports_not_found(self, stub_run, monkeypatch):
        stub_run["run"] = _StubRun(spawned_by=_CALLER)

        async def _empty(session_id, graph=None):
            return []

        monkeypatch.setattr("agent.state_port.read_messages", _empty)

        out = await sm.read_session_transcript("owner", _CHILD)

        assert out.startswith("No messages found")
        assert _CHILD in out

    @pytest.mark.asyncio
    async def test_denied_callers_get_the_reason_not_the_transcript(self, stub_run):
        stub_run["run"] = _StubRun(spawned_by=_CALLER)

        out = await sm.read_session_transcript("intruder", _CHILD)

        assert out.startswith("Cannot read session:")
        assert "not the spawning session" in out

    @pytest.mark.asyncio
    async def test_only_the_last_limit_messages_are_rendered(self, stub_run, monkeypatch):
        stub_run["run"] = _StubRun(spawned_by=_CALLER)
        history = [HumanMessage(content=f"m{i}") for i in range(12)]

        async def _read(session_id, graph=None):
            return history

        monkeypatch.setattr("agent.state_port.read_messages", _read)

        out = await sm.read_session_transcript("owner", _CHILD, limit=3)

        assert "m9" in out and "m11" in out
        assert "m8" not in out
        assert "Showing the last 3 message(s)" in out

    @pytest.mark.asyncio
    async def test_the_query_filter_keeps_only_matching_messages(self, stub_run, monkeypatch):
        stub_run["run"] = _StubRun(spawned_by=_CALLER)
        history = [
            HumanMessage(content="run the tests"),
            AIMessage(content="fatal error: boom"),
            AIMessage(content="all green"),
        ]

        async def _read(session_id, graph=None):
            return history

        monkeypatch.setattr("agent.state_port.read_messages", _read)

        out = await sm.read_session_transcript("owner", _CHILD, query="error")

        assert "fatal error: boom" in out
        assert "all green" not in out

    @pytest.mark.asyncio
    async def test_a_query_matching_nothing_says_so(self, stub_run, monkeypatch):
        stub_run["run"] = _StubRun(spawned_by=_CALLER)

        async def _read(session_id, graph=None):
            return [HumanMessage(content="hello")]

        monkeypatch.setattr("agent.state_port.read_messages", _read)

        out = await sm.read_session_transcript("owner", _CHILD, query="zzz")

        assert "matches" in out


class TestRuntimeWiring:
    """The model-facing tool forwards the INJECTED session_id as the caller."""

    @pytest.mark.asyncio
    async def test_the_injected_session_id_is_the_caller_identity(self, stub_run, monkeypatch):
        from agent.tools.subagent.tools.runtime_tools import sessions_messages_runtime_tool

        stub_run["run"] = _StubRun(spawned_by="agent:main:session:owner")

        async def _read(session_id, graph=None):
            return [AIMessage(content="partial progress")]

        monkeypatch.setattr("agent.state_port.read_messages", _read)

        out = await sessions_messages_runtime_tool.coroutine(
            target_session_key=_CHILD, session_id="owner", limit=5
        )

        assert "partial progress" in out

        denied = await sessions_messages_runtime_tool.coroutine(
            target_session_key=_CHILD, session_id="someone-else"
        )
        assert denied.startswith("Cannot read session:")

    def test_the_schema_documents_the_key_and_the_limit(self):
        from agent.tools.subagent.tools.runtime_tools import sessions_messages_runtime_tool

        props = sessions_messages_runtime_tool.args_schema.model_json_schema()["properties"]

        assert "session_key=" in props["target_session_key"]["description"]
        assert "[1, 30]" in props["limit"]["description"]


def test_the_module_never_raises_on_a_broken_read(monkeypatch, stub_run):
    """A checkpoint-read failure is reported as text, never raised into a turn."""
    stub_run["run"] = _StubRun(spawned_by=_CALLER)

    async def _boom(session_id, graph=None):
        raise RuntimeError("checkpoint db is gone")

    monkeypatch.setattr("agent.state_port.read_messages", _boom)

    out = asyncio.run(sm.read_session_transcript("owner", _CHILD))

    assert out.startswith("Cannot read session:")
    assert "checkpoint read failed" in out
