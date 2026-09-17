"""Unit tests for pre-replacement persistence of compaction-discarded messages.

Covers item 3 + item 5 of TODAY_TODO:

- write-once across a T2 (request-only override) flush followed by a T1
  (state write-back) flush of the same slice — exact row count;
- write-once across a simulated process restart (in-process ``_db_persisted``
  markers cleared, checkpoint-deserialized state replayed) — the persistent
  ``persisted_message_ids`` watermark filters the replay;
- original-content guarantee: a tool output rewritten by the non-LLM strategies
  (dedup placeholder) is persisted in its ORIGINAL form;
- ordering: the flush runs before ``_build_new_messages`` / ``request.override``;
- compression-time nudge dispatch (memory review + plan extraction) replaces
  the removed after-agent dispatch;
- sync and async compression paths both covered.

Isolation: the MesMemory DB is redirected to a tmp file per test (same
``isolated_db`` pattern as tests/context_engine/store) and every session id is
unique, so no row can leak between tests.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agent.middlewares.context_engine.nudge as nudge_mod
import agent.middlewares.summarization.compaction_persistence as cp_mod
from agent.middlewares.context_engine import core as ce_core
from agent.middlewares.summarization import Summarization
from context_engine.store import core as store_core
from context_engine.store import db as store_db
from context_engine.store.core import get_history_by_turn_page
from runtime import state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class _StubSummaryModel:
    """Fake auxiliary summary model (sync + async)."""

    _llm_type = "fake"

    def invoke(self, prompt: Any, config: Any = None) -> Any:  # noqa: ARG002
        return self._response()

    async def ainvoke(self, prompt: Any, config: Any = None) -> Any:  # noqa: ARG002
        return self._response()

    @staticmethod
    def _response() -> Any:
        return type(
            "R",
            (),
            {
                "content": (
                    "A sufficiently long deterministic summary of the conversation "
                    "history covering decisions, files and next steps."
                )
            },
        )()


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "mes_memory.db"
    monkeypatch.setattr(store_db, "_db_path", db_path)
    monkeypatch.setattr(store_db, "_db", None)
    monkeypatch.setattr(store_core, "_db", store_db.get_db())
    return db_path


@pytest.fixture
def sid(request: pytest.FixtureRequest) -> str:
    value = "cmp-persist-" + request.node.name[:32] + "-" + uuid.uuid4().hex[:6]
    yield value
    state_register_mem.clear_session(value)


def _large_history(turns: int = 60, out_chars: int = 800) -> list:
    """Deterministic history long enough to force a real cutoff."""
    messages: list = []
    for i in range(turns):
        messages.append(HumanMessage(content=f"question {i}", id=f"h{i}"))
        messages.append(
            AIMessage(
                content=f"working {i}",
                id=f"a{i}",
                tool_calls=[
                    {
                        "name": "terminal",
                        "args": {"command": f"cmd {i}"},
                        "id": f"c{i}",
                        "type": "tool_call",
                    }
                ],
            )
        )
        messages.append(
            ToolMessage(
                content=f"output {i} " + "x" * out_chars,
                name="terminal",
                tool_call_id=f"c{i}",
                id=f"t{i}",
            )
        )
    return messages


def _request(messages: list, session_id: str) -> ModelRequest:
    return ModelRequest(
        model=_StubSummaryModel(),
        messages=list(messages),
        state={"session_id": session_id, "messages": list(messages)},
    )


def _make_summarization() -> Summarization:
    return Summarization(
        model=_StubSummaryModel(),
        trigger=[("tokens", 5_000)],
        main_llm_context_window=2_000,
    )


def _row_count(session_id: str) -> int:
    row = store_core._db.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
    ).fetchone()
    return int(row[0])


def _watermark_count(session_id: str) -> int:
    row = store_core._db.execute(
        "SELECT COUNT(*) FROM persisted_message_ids WHERE session_id = ?", (session_id,)
    ).fetchone()
    return int(row[0])


def _rows_by_tool_call_id(session_id: str) -> dict[str, dict]:
    rows = get_history_by_turn_page(session_id, turn_page_size=1000)
    return {str(row["tool_call_id"]): row for row in rows if row.get("tool_call_id") is not None}


def _clear_persisted_markers(messages: list) -> None:
    """Simulate a restart: checkpoint-deserialized messages lose the marker."""
    for message in messages:
        message.additional_kwargs.pop("_db_persisted", None)


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Write-once across compression paths
# ---------------------------------------------------------------------------


class TestNoDuplicateWrites:
    @pytest.mark.asyncio
    async def test_t2_then_t1_replay_writes_each_message_once(self, isolated_db, sid):
        messages = _large_history()
        summarization = _make_summarization()

        # Given a T2 compression (request-only override, no state write-back).
        first = await summarization._aapply_compression_under_lock(_request(messages, sid), sid)
        rows_after_t2 = _row_count(sid)

        # When the same slice is compressed again (T1 write-back path).
        second = await summarization._aapply_compression_under_lock(_request(messages, sid), sid)

        # Then every message was written exactly once and no new row appeared.
        assert len(first.messages) < len(messages)
        assert len(second.messages) < len(messages)
        assert rows_after_t2 > 0
        assert _row_count(sid) == rows_after_t2

    @pytest.mark.asyncio
    async def test_restart_replay_writes_nothing(self, isolated_db, sid):
        messages = _large_history()
        summarization = _make_summarization()

        await summarization._aapply_compression_under_lock(_request(messages, sid), sid)
        rows_after_first = _row_count(sid)
        watermark_after_first = _watermark_count(sid)
        assert watermark_after_first > 0

        # Given a restart: in-process markers are gone, the checkpoint state
        # still carries the same message objects (now with the same ids).
        _clear_persisted_markers(messages)

        # When compression replays the same slice.
        await summarization._aapply_compression_under_lock(_request(messages, sid), sid)

        # Then the persistent watermark filters everything out.
        assert _row_count(sid) == rows_after_first
        assert _watermark_count(sid) == watermark_after_first

    def test_sync_t2_then_t1_writes_each_message_once(self, isolated_db, sid):
        messages = _large_history()
        summarization = _make_summarization()

        first = summarization._apply_compression_under_lock(_request(messages, sid), sid)
        rows_after_first = _row_count(sid)

        second = summarization._apply_compression_under_lock(_request(messages, sid), sid)

        assert len(first.messages) < len(messages)
        assert len(second.messages) < len(messages)
        assert rows_after_first > 0
        assert _row_count(sid) == rows_after_first


# ---------------------------------------------------------------------------
# Original-content guarantee
# ---------------------------------------------------------------------------


class TestOriginalContent:
    @pytest.mark.asyncio
    async def test_deduped_tool_output_persists_original_content(self, isolated_db, sid):
        original = "ORIGINAL-OUTPUT " + "y" * 3_000
        duplicated_pair = [
            HumanMessage(content="q-dup", id="dh0"),
            AIMessage(
                content="",
                id="da0",
                tool_calls=[
                    {
                        "name": "terminal",
                        "args": {"command": "same"},
                        "id": "dc0",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content=original, name="terminal", tool_call_id="dc0", id="dt0"),
            HumanMessage(content="q-dup-2", id="dh1"),
            AIMessage(
                content="",
                id="da1",
                tool_calls=[
                    {
                        "name": "terminal",
                        "args": {"command": "same"},
                        "id": "dc1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content=original, name="terminal", tool_call_id="dc1", id="dt1"),
        ]
        messages = [*duplicated_pair, *_large_history(turns=30, out_chars=800)]
        summarization = _make_summarization()

        # The non-LLM strategies really rewrite the earlier duplicate copy...
        transformed, reduced = summarization._run_non_llm_strategies(list(messages), sid)
        assert reduced > 0
        transformed_tools = {m.tool_call_id: m for m in transformed if isinstance(m, ToolMessage)}
        assert transformed_tools["dc0"].content != original
        assert messages[2].content == original

        # ...but the persisted row keeps the ORIGINAL content.
        await summarization._aapply_compression_under_lock(_request(messages, sid), sid)

        rows = _rows_by_tool_call_id(sid)
        assert rows["dc0"]["content"] == original
        assert rows["dc1"]["content"] == original

    def test_sync_path_persists_original_content(self, isolated_db, sid):
        original = "ORIGINAL-OUTPUT " + "z" * 3_000
        messages = [
            HumanMessage(content="q-dup", id="sh0"),
            AIMessage(
                content="",
                id="sa0",
                tool_calls=[
                    {
                        "name": "terminal",
                        "args": {"command": "same"},
                        "id": "sc0",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content=original, name="terminal", tool_call_id="sc0", id="st0"),
            HumanMessage(content="q-dup-2", id="sh1"),
            AIMessage(
                content="",
                id="sa1",
                tool_calls=[
                    {
                        "name": "terminal",
                        "args": {"command": "same"},
                        "id": "sc1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content=original, name="terminal", tool_call_id="sc1", id="st1"),
            *_large_history(turns=30, out_chars=800),
        ]
        summarization = _make_summarization()

        summarization._apply_compression_under_lock(_request(messages, sid), sid)

        rows = _rows_by_tool_call_id(sid)
        assert rows["sc0"]["content"] == original


# ---------------------------------------------------------------------------
# Ordering: flush before replacement
# ---------------------------------------------------------------------------


class TestPersistenceOrdering:
    @pytest.mark.asyncio
    async def test_add_messages_runs_before_build_and_override(self, isolated_db, sid, monkeypatch):
        order: list[str] = []
        real_add = cp_mod.add_messages

        async def _spy_add(session_id: str, messages: list) -> None:
            order.append("persist")
            await real_add(session_id, messages)

        real_build = Summarization._build_new_messages

        def _spy_build(self, summary: str) -> list:
            order.append("build")
            return real_build(self, summary)

        real_override = ModelRequest.override

        def _spy_override(self, **overrides: Any) -> ModelRequest:
            order.append("override")
            return real_override(self, **overrides)

        monkeypatch.setattr(cp_mod, "add_messages", _spy_add)
        monkeypatch.setattr(Summarization, "_build_new_messages", _spy_build)
        monkeypatch.setattr(ModelRequest, "override", _spy_override)

        await _make_summarization()._aapply_compression_under_lock(
            _request(_large_history(), sid), sid
        )

        assert "persist" in order
        assert "build" in order
        assert "override" in order
        assert order.index("persist") < order.index("build")
        assert order.index("persist") < order.index("override")

    def test_sync_add_messages_sync_runs_before_build_and_override(
        self, isolated_db, sid, monkeypatch
    ):
        order: list[str] = []
        real_add = cp_mod.add_messages_sync

        def _spy_add(session_id: str, messages: list) -> None:
            order.append("persist")
            real_add(session_id, messages)

        real_build = Summarization._build_new_messages

        def _spy_build(self, summary: str) -> list:
            order.append("build")
            return real_build(self, summary)

        real_override = ModelRequest.override

        def _spy_override(self, **overrides: Any) -> ModelRequest:
            order.append("override")
            return real_override(self, **overrides)

        monkeypatch.setattr(cp_mod, "add_messages_sync", _spy_add)
        monkeypatch.setattr(Summarization, "_build_new_messages", _spy_build)
        monkeypatch.setattr(ModelRequest, "override", _spy_override)

        _make_summarization()._apply_compression_under_lock(_request(_large_history(), sid), sid)

        assert order.index("persist") < order.index("build")
        assert order.index("persist") < order.index("override")


# ---------------------------------------------------------------------------
# Compression-time nudge dispatch (item 5)
# ---------------------------------------------------------------------------


class _FakeStateRegister:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], object] = {}

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self.data.get((session_id, key), default)

    def set_state(self, session_id: str, key: str, value: object) -> None:
        self.data[(session_id, key)] = value


class TestCompressionNudgeDispatch:
    @pytest.mark.asyncio
    async def test_compression_dispatches_memory_and_plan_nudges(
        self, isolated_db, sid, monkeypatch
    ):
        calls: list[str] = []

        async def _memory(session_id: str, system_prompt: str, messages: list) -> None:
            calls.append("memory")

        async def _plan(session_id: str, system_prompt: str, messages: list) -> None:
            calls.append("plan")

        monkeypatch.setattr(nudge_mod, "_nudge_memory", _memory)
        monkeypatch.setattr(nudge_mod, "_nudge_plan_extraction", _plan)
        monkeypatch.setattr(nudge_mod, "_detect_todo_all_complete", lambda session_id: True)
        monkeypatch.setattr(
            ce_core,
            "_get_and_reload_system_prompt",
            lambda session_id: "sys-prompt",
        )
        fake_db = _FakeStateRegister()
        fake_db.set_state(
            sid, nudge_mod._NUDGE_MEMORY_COUNT_KEY, nudge_mod._NUDGE_MEMORY_THRESHOLD - 1
        )
        monkeypatch.setattr(nudge_mod, "state_register_db", fake_db)

        await _make_summarization()._aapply_compression_under_lock(
            _request(_large_history(), sid), sid
        )
        await _settle()

        assert calls == ["memory", "plan"]
        assert fake_db.get_state(sid, nudge_mod._NUDGE_MEMORY_COUNT_KEY, 0) == 0

    @pytest.mark.asyncio
    async def test_no_cut_does_not_dispatch_nudges(self, isolated_db, sid, monkeypatch):
        calls: list[str] = []

        async def _memory(session_id: str, system_prompt: str, messages: list) -> None:
            calls.append("memory")

        async def _plan(session_id: str, system_prompt: str, messages: list) -> None:
            calls.append("plan")

        monkeypatch.setattr(nudge_mod, "_nudge_memory", _memory)
        monkeypatch.setattr(nudge_mod, "_nudge_plan_extraction", _plan)
        monkeypatch.setattr(nudge_mod, "_detect_todo_all_complete", lambda session_id: True)
        monkeypatch.setattr(Summarization, "_determine_cutoff", lambda self, messages: 0)

        await _make_summarization()._aapply_compression_under_lock(
            _request(_large_history(turns=6, out_chars=10), sid), sid
        )
        await _settle()

        assert calls == []
