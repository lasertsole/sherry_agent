"""Unit tests for per-model-boundary message persistence.

Locks the new persistence timing: ``MessagePersistenceMiddleware.after_model`` /
``aafter_model`` flush every new graph-state message (human / ai / tool) into
MesMemory at each model boundary, write-once via ``persisted_message_ids``.

Coverage:

- each message lands exactly once across a full human -> ai(tool_call) -> tool
  -> ai round;
- three consecutive boundaries only grow the row count by the new messages
  (state accumulation never rewrites an already-persisted message);
- process-restart replay (in-process ``_db_persisted`` markers cleared) adds no
  row — the persistent watermark filters it;
- sync and async hooks cover the same key cases;
- missing/blank ``session_id`` skips without writing or raising;
- HITL-denied tool calls keep their re-attached pair in the stored rows;
- filter semantics: ``_db_persisted`` / ``lc_source == "summarization"`` /
  non human|ai|tool messages are not persisted.
"""

from __future__ import annotations

import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.middlewares.message_persistence import core as mp_core
from agent.middlewares.message_persistence import MessagePersistenceMiddleware
from context_engine.store import core as store_core
from context_engine.store import db as store_db
from context_engine.store.core import get_history_by_turn_page
from runtime import state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "mes_memory.db"
    monkeypatch.setattr(store_db, "_db_path", db_path)
    monkeypatch.setattr(store_db, "_db", None)
    monkeypatch.setattr(store_core, "_db", store_db.get_db())
    return db_path


@pytest.fixture
def sid(request: pytest.FixtureRequest) -> str:
    value = "msg-persist-" + request.node.name[:32] + "-" + uuid.uuid4().hex[:6]
    yield value
    state_register_mem.clear_session(value)


def _state(session_id: str, messages: list) -> dict:
    return {"session_id": session_id, "messages": list(messages)}


async def _run_boundary(
    middleware: MessagePersistenceMiddleware, state: dict, *, sync: bool
) -> None:
    if sync:
        assert middleware.after_model(state, None) is None
    else:
        assert await middleware.aafter_model(state, None) is None


def _one_round() -> list:
    return [
        HumanMessage(content="hello", id="h1"),
        AIMessage(
            content="let me check",
            id="a1",
            tool_calls=[
                {
                    "name": "terminal",
                    "args": {"command": "ls"},
                    "id": "c1",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content="file list", name="terminal", tool_call_id="c1", id="t1"),
        AIMessage(content="all done", id="a2"),
    ]


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


def _contents(session_id: str) -> list[str]:
    rows = get_history_by_turn_page(session_id, turn_page_size=1000)
    return sorted(str(row["content"]) for row in rows)


def _clear_persisted_markers(messages: list) -> None:
    """Simulate a restart: checkpoint-deserialized messages lose the marker."""
    for message in messages:
        message.additional_kwargs.pop(store_core._DB_PERSISTED_KEY, None)


class TestExactlyOnce:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
    async def test_full_round_writes_each_message_once(self, isolated_db, sid, sync):
        middleware = MessagePersistenceMiddleware()
        messages = _one_round()

        # Given three model boundaries seeing the accumulating state:
        # human -> ai(tool_call) | tool -> ai
        await _run_boundary(middleware, _state(sid, messages[:2]), sync=sync)
        await _run_boundary(middleware, _state(sid, messages[:3]), sync=sync)
        await _run_boundary(middleware, _state(sid, messages), sync=sync)

        # Then all four messages are stored exactly once each.
        assert _row_count(sid) == 4
        assert _contents(sid) == [
            "all done",
            "file list",
            "hello",
            "let me check",
        ]


class TestAcrossBoundaries:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
    async def test_row_count_grows_only_with_new_messages(self, isolated_db, sid, sync):
        middleware = MessagePersistenceMiddleware()
        messages = _one_round()

        await _run_boundary(middleware, _state(sid, messages[:2]), sync=sync)
        assert _row_count(sid) == 2

        # Same boundary replayed: the in-process markers keep it at 2 rows.
        await _run_boundary(middleware, _state(sid, messages[:2]), sync=sync)
        assert _row_count(sid) == 2

        await _run_boundary(middleware, _state(sid, messages[:3]), sync=sync)
        assert _row_count(sid) == 3

        await _run_boundary(middleware, _state(sid, messages), sync=sync)
        await _run_boundary(middleware, _state(sid, messages), sync=sync)
        assert _row_count(sid) == 4


class TestRestartReplay:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
    async def test_replay_after_marker_loss_adds_no_row(self, isolated_db, sid, sync):
        middleware = MessagePersistenceMiddleware()
        messages = _one_round()

        await _run_boundary(middleware, _state(sid, messages), sync=sync)
        rows_after_first = _row_count(sid)
        watermark_after_first = _watermark_count(sid)
        assert rows_after_first == 4
        assert watermark_after_first == 4

        # Given a restart: in-process markers are gone, the checkpoint state
        # still carries the same message objects (same ids).
        _clear_persisted_markers(messages)

        # When the same boundary replays.
        await _run_boundary(middleware, _state(sid, messages), sync=sync)

        # Then the persistent watermark filters everything out.
        assert _row_count(sid) == rows_after_first
        assert _watermark_count(sid) == watermark_after_first


class TestNoSessionId:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
    async def test_missing_or_blank_session_id_skips_silently(
        self, isolated_db, sid, monkeypatch, sync
    ):
        calls: list[tuple[str, list]] = []

        async def _spy_add_async(session_id: str, messages: list) -> None:
            calls.append((session_id, messages))

        def _spy_add_sync(session_id: str, messages: list) -> None:
            calls.append((session_id, messages))

        monkeypatch.setattr(mp_core, "add_messages", _spy_add_async)
        monkeypatch.setattr(mp_core, "add_messages_sync", _spy_add_sync)

        middleware = MessagePersistenceMiddleware()
        messages = _one_round()

        await _run_boundary(middleware, {"messages": list(messages)}, sync=sync)
        await _run_boundary(
            middleware, {"session_id": "   ", "messages": list(messages)}, sync=sync
        )

        assert calls == []


class TestDenialPairing:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
    async def test_denied_tool_call_pair_is_re_attached(self, isolated_db, sid, sync):
        stripped_ai = AIMessage(content="let me run that", id="a-denied", tool_calls=[])
        denial = ToolMessage(
            content="User denied: nope. The user has NOT consented to this action.",
            name="terminal",
            tool_call_id="call_reject_1",
            status="error",
            id="t-denied",
        )
        state = _state(sid, [HumanMessage(content="hi", id="h-denied"), stripped_ai, denial])

        await _run_boundary(MessagePersistenceMiddleware(), state, sync=sync)

        rows = get_history_by_turn_page(sid, turn_page_size=1000)
        ai_rows = [row for row in rows if row["role"] == "ai"]
        tool_rows = [row for row in rows if row["role"] == "tool"]
        assert len(ai_rows) == 1
        assert len(tool_rows) == 1
        assert ai_rows[0]["tool_calls"][0]["id"] == "call_reject_1"
        assert ai_rows[0]["tool_calls"][0]["name"] == "terminal"
        assert tool_rows[0]["tool_call_id"] == "call_reject_1"
        assert tool_rows[0]["tool_status"] == "error"
        # The re-attachment works on a copy — graph state stays untouched.
        assert stripped_ai.tool_calls == []


class TestFilterSemantics:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
    async def test_non_persistable_messages_are_skipped(self, isolated_db, sid, sync):
        already_flushed = AIMessage(content="already flushed", id="fm")
        already_flushed.additional_kwargs[store_core._DB_PERSISTED_KEY] = True
        messages = [
            HumanMessage(content="keep human", id="fh"),
            AIMessage(content="keep ai", id="fa"),
            AIMessage(
                content="summary ai",
                id="fs",
                additional_kwargs={"lc_source": "summarization"},
            ),
            HumanMessage(
                content="summary human",
                id="fsh",
                additional_kwargs={"lc_source": "summarization"},
            ),
            SystemMessage(content="system", id="fsys"),
            already_flushed,
        ]

        await _run_boundary(MessagePersistenceMiddleware(), _state(sid, messages), sync=sync)

        assert _contents(sid) == ["keep ai", "keep human"]
