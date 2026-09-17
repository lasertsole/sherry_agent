"""Tool-result eviction middleware: chain order, three-state split, watermark.

The chain under test mirrors ``agent/core.py``: the eviction middleware is
OUTER and ``MessagePersistenceMiddleware`` is INNER. The inner layer therefore
flushes the RAW result at tool return, and the outer layer swaps in the
preview before it can reach graph state.

Locked invariants:
- state carries the preview, MesMemory carries the full text, and the eviction
  file round-trips byte-identically;
- ``model_copy`` keeps the message id / status / name, and the next model
  boundary writes no second row (marker intact, marker lost with id, marker
  lost without id);
- multimodal non-text blocks survive;
- the P2-4 read_file slice writes no file and stays composable with the
  compression-time clip in ``target_truncation.py``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from agent.middlewares.message_persistence import MessagePersistenceMiddleware
from agent.middlewares.tool_result_eviction import ToolResultEvictionMiddleware
from config.features import TOOL_RESULT_EVICTION
from context_engine.store import core as store_core
from context_engine.store.core import get_history_by_turn_page
from pub.func.message.eviction import (
    _READ_FILE_SLICE_NOTICE,
    load_evicted,
    slice_read_file_result,
)
from pub.func.message.target_truncation import target_truncate_tool_outputs

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]

THRESHOLD = TOOL_RESULT_EVICTION["evict_threshold_chars"]
# A read_file-shaped payload is valid JSON; the exec slice breaks it, which is
# exactly what the compression-time path must survive.
READ_FILE_CONTENT = '{"content": "' + ("y" * 60 + "\\n") * 300 + '", "total_lines": 300}'


def _big_content(extra: int = 0) -> str:
    return ("x" * 99 + "\n") * ((THRESHOLD + extra) // 100) + "x" * ((THRESHOLD + extra) % 100)


def _big_tool_message(
    tool_call_id: str = "c1", *, name: str = "terminal", msg_id: str | None = None
) -> ToolMessage:
    return ToolMessage(content=_big_content(5_000), name=name, tool_call_id=tool_call_id, id=msg_id)


def _request(session_id: str, tool_call_id: str = "c1") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={
            "name": "terminal",
            "args": {"command": "ls"},
            "id": tool_call_id,
            "type": "tool_call",
        },
        tool=None,
        state={"session_id": session_id, "messages": []},
        runtime=None,
    )


async def _async_return(value: Any) -> Any:
    return value


def _async_handler(value: Any):
    async def handler(_request: ToolCallRequest) -> Any:
        return value

    return handler


def _sync_handler(value: Any):
    def handler(_request: ToolCallRequest) -> Any:
        return value

    return handler


async def _run_chain(
    eviction: ToolResultEvictionMiddleware,
    persistence: MessagePersistenceMiddleware,
    request: ToolCallRequest,
    message: ToolMessage,
    *,
    sync: bool,
) -> Any:
    """Run the production order: eviction (outer) → persistence (inner) → tool."""
    if sync:
        return eviction.wrap_tool_call(
            request,
            lambda inner_request: persistence.wrap_tool_call(
                inner_request, lambda _request: message
            ),
        )
    return await eviction.awrap_tool_call(
        request,
        lambda inner_request: persistence.awrap_tool_call(inner_request, _async_handler(message)),
    )


def _eviction_path(preview: str) -> Path:
    match = re.search(r"\[evicted to: (.*?)\]", preview)
    assert match is not None, f"preview carries no eviction path: {preview[:120]}"
    return Path(match.group(1))


def _tool_rows(session_id: str) -> list[dict]:
    return [
        row
        for row in get_history_by_turn_page(session_id, turn_page_size=1000)
        if row["role"] == "tool"
    ]


def _row_count(session_id: str) -> int:
    row = store_core._db.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
    ).fetchone()
    return int(row[0])


def _tool_row_count(session_id: str, tool_call_id: str) -> int:
    row = store_core._db.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ? AND tool_call_id = ?",
        (session_id, tool_call_id),
    ).fetchone()
    return int(row[0])


def _watermark_count(session_id: str) -> int:
    row = store_core._db.execute(
        "SELECT COUNT(*) FROM persisted_message_ids WHERE session_id = ?", (session_id,)
    ).fetchone()
    return int(row[0])


def _boundary_state(session_id: str, response: ToolMessage) -> dict:
    return {
        "session_id": session_id,
        "messages": [
            HumanMessage(content="run it", id="h1"),
            AIMessage(
                content="calling",
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
            response,
        ],
    }


class TestThreeStateSplit:
    @pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
    @pytest.mark.asyncio
    async def test_state_preview_mesmemory_full_disk_full(
        self, isolated_db, isolated_sessions, sid, sync
    ):
        eviction = ToolResultEvictionMiddleware()
        persistence = MessagePersistenceMiddleware()
        original = _big_tool_message(msg_id="t1")
        full_text = original.content

        response = await _run_chain(eviction, persistence, _request(sid), original, sync=sync)

        # state (and therefore the checkpointer) holds the preview only.
        assert response is not original
        assert response.id == "t1"
        assert response.tool_call_id == "c1"
        assert response.content.startswith("[evicted to: ")
        assert len(response.content) < len(full_text)
        # the eviction file round-trips byte-identically.
        assert load_evicted(_eviction_path(response.content)) == full_text
        # MesMemory holds the full text, written by the inner layer first.
        tool_rows = _tool_rows(sid)
        assert len(tool_rows) == 1
        assert tool_rows[0]["content"] == full_text
        assert _watermark_count(sid) == 1

    @pytest.mark.asyncio
    async def test_boundary_after_eviction_writes_no_second_row(
        self, isolated_db, isolated_sessions, sid
    ):
        eviction = ToolResultEvictionMiddleware()
        persistence = MessagePersistenceMiddleware()
        original = _big_tool_message(msg_id="t1")
        response = await _run_chain(eviction, persistence, _request(sid), original, sync=False)

        persistence.after_model(_boundary_state(sid, response), None)

        assert _row_count(sid) == 3
        assert _tool_row_count(sid, "c1") == 1
        assert _tool_rows(sid)[0]["content"] == original.content
        assert _watermark_count(sid) == 3

    @pytest.mark.parametrize("assign_id", [True, False], ids=["id-assigned", "id-missing"])
    @pytest.mark.asyncio
    async def test_restart_replay_without_marker_adds_no_row(
        self, isolated_db, isolated_sessions, sid, assign_id
    ):
        eviction = ToolResultEvictionMiddleware()
        persistence = MessagePersistenceMiddleware()
        original = _big_tool_message(msg_id=None)
        response = await _run_chain(eviction, persistence, _request(sid), original, sync=False)

        # Simulate a checkpoint restart: the in-process marker is lost and the
        # graph reducer has assigned the message id.
        response.additional_kwargs.pop(store_core._DB_PERSISTED_KEY, None)
        if assign_id:
            response.id = "t1"
        persistence.after_model(_boundary_state(sid, response), None)

        assert _row_count(sid) == 3
        assert _tool_row_count(sid, "c1") == 1
        assert _tool_rows(sid)[0]["content"] == original.content


class TestEvictionSkips:
    @pytest.mark.parametrize(
        "tool_name", sorted(TOOL_RESULT_EVICTION["excluded_tools"] - {"read_file"})
    )
    def test_excluded_tool_passes_through_untouched(
        self, isolated_db, isolated_sessions, sid, tool_name
    ):
        eviction = ToolResultEvictionMiddleware()
        original = _big_tool_message(name=tool_name)

        response = eviction.wrap_tool_call(_request(sid), _sync_handler(original))

        assert response is original
        assert not (isolated_sessions / sid / "evicted").exists()

    def test_missing_session_id_passes_through(self, isolated_db, isolated_sessions):
        eviction = ToolResultEvictionMiddleware()
        original = _big_tool_message()
        request = _request("")

        assert eviction.wrap_tool_call(request, _sync_handler(original)) is original

    @pytest.mark.parametrize("bad", ["..", "a/b", "a\\b"])
    def test_unsafe_session_segment_never_writes(self, isolated_db, isolated_sessions, bad):
        eviction = ToolResultEvictionMiddleware()
        original = _big_tool_message()

        response = eviction.wrap_tool_call(_request(bad), _sync_handler(original))

        assert response is original
        assert not isolated_sessions.exists() or not any(isolated_sessions.rglob("*"))

    def test_disabled_flag_is_passthrough(self, isolated_db, isolated_sessions, sid, monkeypatch):
        monkeypatch.setitem(TOOL_RESULT_EVICTION, "enabled", False)
        eviction = ToolResultEvictionMiddleware()
        original = _big_tool_message()

        assert eviction.wrap_tool_call(_request(sid), _sync_handler(original)) is original
        assert not (isolated_sessions / sid).exists()

    def test_second_pass_over_the_preview_is_a_no_op(self, isolated_db, isolated_sessions, sid):
        eviction = ToolResultEvictionMiddleware()
        original = _big_tool_message(msg_id="t1")
        response = eviction.wrap_tool_call(_request(sid), _sync_handler(original))
        assert response.content.startswith("[evicted to: ")

        assert eviction._maybe_evict(response, sid) is response


class TestMultimodalAndResponseShapes:
    def test_multimodal_blocks_survive_in_state(self, isolated_db, isolated_sessions, sid):
        eviction = ToolResultEvictionMiddleware()
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        original = ToolMessage(
            content=[{"type": "text", "text": _big_content(5_000)}, image],
            name="terminal",
            tool_call_id="c1",
            id="t1",
        )

        response = eviction.wrap_tool_call(_request(sid), _sync_handler(original))

        assert isinstance(response.content, list)
        assert response.content[0]["type"] == "text"
        assert response.content[0]["text"].startswith("[evicted to: ")
        assert response.content[1] == image
        assert response.id == "t1"

    def test_command_and_list_responses_rewrite_their_tool_messages(
        self, isolated_db, isolated_sessions, sid
    ):
        eviction = ToolResultEvictionMiddleware()
        nested = _big_tool_message("c1", msg_id="t1")
        command = Command(update={"messages": [nested]})

        rewritten = eviction.wrap_tool_call(_request(sid, "c1"), _sync_handler(command))

        assert isinstance(rewritten, Command)
        nested_result = rewritten.update["messages"][0]
        assert nested_result.id == "t1"
        assert nested_result.content.startswith("[evicted to: ")

        listed = _big_tool_message("c2", msg_id="t2")
        rewritten_list = eviction.wrap_tool_call(_request(sid, "c2"), _sync_handler([listed]))
        assert isinstance(rewritten_list, list)
        assert rewritten_list[0].id == "t2"
        assert rewritten_list[0].content.startswith("[evicted to: ")

        navigation = Command(goto="tools")
        assert eviction.wrap_tool_call(_request(sid, "c3"), _sync_handler(navigation)) is navigation


class TestReadFileSlice:
    @pytest.mark.asyncio
    async def test_read_file_is_sliced_not_offloaded(self, isolated_db, isolated_sessions, sid):
        eviction = ToolResultEvictionMiddleware()
        persistence = MessagePersistenceMiddleware()
        original = ToolMessage(
            content=READ_FILE_CONTENT, name="read_file", tool_call_id="r1", id="t1"
        )

        response = await _run_chain(
            eviction, persistence, _request(sid, "r1"), original, sync=False
        )

        assert response is not original
        assert response.id == "t1"
        assert response.content == READ_FILE_CONTENT[:4_000] + _READ_FILE_SLICE_NOTICE
        assert not (isolated_sessions / sid / "evicted").exists()
        assert _tool_rows(sid)[0]["content"] == READ_FILE_CONTENT

    def test_read_file_within_the_slice_limit_is_untouched(
        self, isolated_db, isolated_sessions, sid
    ):
        eviction = ToolResultEvictionMiddleware()
        original = ToolMessage(content="z" * 3_000, name="read_file", tool_call_id="r1", id="t1")

        assert eviction.wrap_tool_call(_request(sid, "r1"), _sync_handler(original)) is original

    def test_second_pass_over_the_slice_is_a_no_op(self, isolated_db, isolated_sessions, sid):
        eviction = ToolResultEvictionMiddleware()
        original = ToolMessage(
            content=READ_FILE_CONTENT, name="read_file", tool_call_id="r1", id="t1"
        )
        sliced = eviction.wrap_tool_call(_request(sid, "r1"), _sync_handler(original))
        assert sliced.content.endswith(_READ_FILE_SLICE_NOTICE)

        assert eviction._maybe_evict(sliced, sid) is sliced

    def test_exec_slice_composes_with_the_compression_clip(self):
        """The staged reduction is additive, never conflicting.

        The compression-time clip cannot parse the already-sliced payload, so
        it deterministically falls back to its restart notice; the exec-time
        notice survives exactly once in the kept tail.
        """
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "args": {"file_path": "big.md"}, "id": "r1"}],
        )
        sliced = slice_read_file_result(
            ToolMessage(content=READ_FILE_CONTENT, name="read_file", tool_call_id="r1")
        )

        result, reduced = target_truncate_tool_outputs(
            [ai, sliced],
            target_reduction_tokens=10**6,
            min_output_chars=500,
            max_output_chars=2_000,
        )

        clipped = result[1].content
        assert reduced > 0
        assert "the file is unchanged on disk" in clipped
        assert "Re-read it from the start in chunks" in clipped
        assert clipped.count("Output was truncated due to eviction threshold") == 1
        assert clipped.count("the file is unchanged on disk") == 1
