"""Reasoning-tokens tracking tests.

Covers the reasoning-token chain end to end: extraction from
``usage_metadata.output_token_details`` in StreamTurn, the ``meta`` frame
field, the StreamDriver ``done`` frame field, and persistence through
``add_messages`` (nullable column; old rows and non-reasoning models stay
NULL).
"""

import asyncio
import sqlite3
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from context_engine.store import core as store_core
from context_engine.store.db import _migrate
from server.service import messages as m
from server.service.stream_driver import StreamDriver
from pub.types.message import MultiModalMessage

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SID = "sess-reasontok-1"
_META = {"langgraph_node": "model"}


def _mc(chunk: AIMessageChunk) -> tuple[str, Any]:
    return ("messages", (chunk, dict(_META)))


def _source(chunks):
    async def _gen():
        for c in chunks:
            yield c

    return _gen()


async def _collect(agen) -> list[dict]:
    return [f async for f in agen]


@pytest.fixture()
def store_db(monkeypatch, tmp_path) -> sqlite3.Connection:
    """An isolated, fully migrated DB wired into store_core._db (test_store_origin pattern)."""
    conn = sqlite3.connect(
        tmp_path / "mes_memory_test.db",
        check_same_thread=False,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    monkeypatch.setattr(store_core, "_db", conn)
    yield conn
    conn.close()


class _StreamAgent:
    def __init__(self, chunks):
        self._chunks = chunks

    def astream(self, *args, **kwargs):
        return _source(self._chunks)


class _InvokeAgent:
    def __init__(self, result):
        self._result = result

    async def ainvoke(self, *args, **kwargs):
        return self._result


def _gen_turn(chunks=None, invoke_result=None) -> m._GenerateTurn:
    turn = m._GenerateTurn(
        SID, MultiModalMessage(text="q"), is_stream=chunks is not None, origin=None
    )

    async def _noop_prepare():
        return None

    turn._prepare = _noop_prepare
    if chunks is not None:
        turn._agent = _StreamAgent(chunks)
    else:
        turn._agent = _InvokeAgent(invoke_result)
    return turn


def _meta(frames) -> dict:
    return [f for f in frames if f.get("type") == "meta"][0]


class TestReasoningTokenExtraction:
    def test_stream_usage_reasoning_tokens_reach_meta_frame(self):
        turn = _gen_turn(
            chunks=[
                _mc(
                    AIMessageChunk(
                        content="answer",
                        usage_metadata={
                            "input_tokens": 100,
                            "output_tokens": 60,
                            "total_tokens": 160,
                            "output_token_details": {"reasoning_tokens": 50},
                        },
                    )
                )
            ]
        )
        frames = asyncio.run(_collect(turn.run()))

        meta = _meta(frames)
        assert meta["reasoning_tokens"] == 50
        assert meta["output_tokens"] == 60

    def test_meta_frame_defaults_to_zero_without_details(self):
        turn = _gen_turn(
            chunks=[
                _mc(
                    AIMessageChunk(
                        content="answer",
                        usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
                    )
                )
            ]
        )
        frames = asyncio.run(_collect(turn.run()))

        assert _meta(frames)["reasoning_tokens"] == 0

    def test_invoke_path_extracts_reasoning_tokens(self):
        last = AIMessage(
            content="final",
            usage_metadata={
                "input_tokens": 10,
                "output_tokens": 8,
                "total_tokens": 18,
                "output_token_details": {"reasoning_tokens": 6},
            },
        )
        frames = asyncio.run(_collect(_gen_turn(invoke_result={"messages": [last]}).run()))

        assert _meta(frames)["reasoning_tokens"] == 6


class TestDoneFrameReasoningTokens:
    def test_done_frame_carries_reasoning_tokens_from_meta(self):
        class _RecordingDriver(StreamDriver):
            def __init__(self):
                super().__init__(SID, websocket=object())
                self.frames: list[dict] = []

            async def send_frame(self, payload):
                self.frames.append(payload)

            async def check_interrupt(self):
                return None

            async def on_finish(self):
                return None

        driver = _RecordingDriver()
        chunks = [
            {"type": "text", "content": "x"},
            {
                "type": "meta",
                "content": "",
                "model_name": "glm-5",
                "input_tokens": 11,
                "output_tokens": 22,
                "reasoning_tokens": 15,
                "finish_reason": "stop",
            },
        ]

        async def _source():
            for c in chunks:
                yield c

        asyncio.run(driver.drive(_source()))

        assert driver.frames[-1]["reasoning_tokens"] == 15
        assert driver.frames[-1]["event"] == "done"

    def test_done_frame_defaults_to_zero_without_meta(self):
        class _RecordingDriver(StreamDriver):
            def __init__(self):
                super().__init__(SID, websocket=object())
                self.frames: list[dict] = []

            async def send_frame(self, payload):
                self.frames.append(payload)

            async def check_interrupt(self):
                return None

            async def on_finish(self):
                return None

        driver = _RecordingDriver()

        async def _source():
            yield {"type": "text", "content": "x"}

        asyncio.run(driver.drive(_source()))

        assert driver.frames[-1]["reasoning_tokens"] == 0


class TestReasoningTokenPersistence:
    def test_add_messages_persists_reasoning_tokens(self, store_db):
        msg = AIMessage(
            content="answer",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 60,
                "total_tokens": 160,
                "output_token_details": {"reasoning_tokens": 50},
            },
        )
        asyncio.run(store_core.add_messages(SID, [msg]))

        row = store_db.execute(
            "SELECT reasoning_tokens FROM messages WHERE session_id = ?", (SID,)
        ).fetchone()
        assert row["reasoning_tokens"] == 50

    def test_rows_without_details_persist_null(self, store_db):
        plain = AIMessage(content="no usage")
        human_content = "q"
        from langchain_core.messages import HumanMessage

        asyncio.run(store_core.add_messages(SID, [HumanMessage(content=human_content), plain]))

        rows = store_db.execute(
            "SELECT role, reasoning_tokens FROM messages WHERE session_id = ?", (SID,)
        ).fetchall()
        assert {r["reasoning_tokens"] for r in rows} == {None}

    def test_old_schema_rows_stay_null_safe(self, store_db):
        # Simulate a row written before the column existed: the nullable
        # column reads back NULL and add_messages on the migrated schema works.
        store_db.execute(
            "INSERT INTO messages (turn_num, session_id, role, content, timestamp, ts_ms) "
            "VALUES (1, ?, 'ai', '\"old\"', '20260101000000', 0)",
            (SID,),
        )
        row = store_db.execute(
            "SELECT reasoning_tokens FROM messages WHERE session_id = ?", (SID,)
        ).fetchone()
        assert row["reasoning_tokens"] is None

        msg = AIMessage(
            content="new",
            usage_metadata={
                "input_tokens": 0,
                "output_tokens": 7,
                "total_tokens": 7,
                "output_token_details": {"reasoning_tokens": 7},
            },
        )
        asyncio.run(store_core.add_messages(SID, [msg]))
        rows = store_db.execute(
            "SELECT reasoning_tokens FROM messages WHERE session_id = ? ORDER BY id",
            (SID,),
        ).fetchall()
        assert rows[0]["reasoning_tokens"] is None
        assert rows[1]["reasoning_tokens"] == 7
