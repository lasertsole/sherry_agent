"""Zero-latency persistence: tool results are flushed the moment they return.

``MessagePersistenceMiddleware`` now also implements ``wrap_tool_call`` /
``awrap_tool_call``: the handler runs first, the response is scanned for
``ToolMessage``s (bare, nested in a ``Command``, or in a list — the shapes
langgraph's tool node can return), and they go through the SAME batch
pipeline as ``after_model`` (role/marker filter -> watermark -> prepare ->
write -> mark), then the response is returned untouched. HITL denials
short-circuit OUTSIDE this layer, so they land at the next model boundary.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from agent.middlewares.humanInTheLoop import HITLConfig, HumanInTheLoop
from agent.middlewares.message_persistence import MessagePersistenceMiddleware
from agent.middlewares.message_persistence import core as mp_core
from context_engine.store import core as store_core
from context_engine.store.core import get_history_by_turn_page

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]


def _state(session_id: str, messages: list) -> dict:
    return {"session_id": session_id, "messages": list(messages)}


async def _run_boundary(
    middleware: MessagePersistenceMiddleware, state: dict, *, sync: bool
) -> None:
    if sync:
        assert middleware.after_model(state, None) is None
    else:
        assert await middleware.aafter_model(state, None) is None


def _tool_request(session_id: str, tool_call_id: str = "c1") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={
            "name": "terminal",
            "args": {"command": "ls"},
            "id": tool_call_id,
            "type": "tool_call",
        },
        tool=None,
        state=_state(session_id, []),
        runtime=None,
    )


def _sync_handler(response: Any) -> Callable[[ToolCallRequest], Any]:
    def handler(_request: ToolCallRequest) -> Any:
        return response

    return handler


def _async_handler(response: Any) -> Callable[[ToolCallRequest], Awaitable[Any]]:
    async def handler(_request: ToolCallRequest) -> Any:
        return response

    return handler


async def _run_wrap(
    middleware: MessagePersistenceMiddleware,
    request: ToolCallRequest,
    response: Any,
    *,
    sync: bool,
) -> Any:
    if sync:
        return middleware.wrap_tool_call(request, _sync_handler(response))
    return await middleware.awrap_tool_call(request, _async_handler(response))


def _tool_message(tool_call_id: str, *, content: str | None = None, msg_id: str | None = None):
    return ToolMessage(
        content=content or f"output {tool_call_id}",
        name="terminal",
        tool_call_id=tool_call_id,
        id=msg_id,
    )


def _ai_with_calls(tool_call_ids: list[str], *, content: str) -> AIMessage:
    return AIMessage(
        content=content,
        id="a-" + "-".join(tool_call_ids),
        tool_calls=[
            {
                "name": "terminal",
                "args": {"command": "ls"},
                "id": call_id,
                "type": "tool_call",
            }
            for call_id in tool_call_ids
        ],
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


def _rows(session_id: str) -> list[dict]:
    return get_history_by_turn_page(session_id, turn_page_size=1000)


def _tool_row_count(session_id: str, tool_call_id: str) -> int:
    row = store_core._db.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ? AND tool_call_id = ?",
        (session_id, tool_call_id),
    ).fetchone()
    return int(row[0])


def _duplicate_row_groups(session_id: str) -> list[tuple]:
    rows = store_core._db.execute(
        "SELECT role, content, tool_call_id, COUNT(*) AS n FROM messages "
        "WHERE session_id = ? GROUP BY role, content, tool_call_id HAVING n > 1",
        (session_id,),
    ).fetchall()
    return [tuple(row) for row in rows]


def _assert_no_duplicate_rows(session_id: str, expected_rows: int) -> None:
    """Invariant: no two stored rows map to the same LangGraph message.

    Fixtures give every message a unique ``(role, content, tool_call_id)`` —
    identical tool output across different calls stays distinguishable by
    ``tool_call_id`` — so a GROUP BY on that triple with ``HAVING COUNT(*) > 1``
    is exactly "two rows for one message", and the row count must equal the
    number of distinct messages the middleware was handed.
    """
    assert _row_count(session_id) == expected_rows
    assert _duplicate_row_groups(session_id) == []


def _clear_persisted_markers(messages: list) -> None:
    """Simulate a restart: checkpoint-deserialized messages lose the marker."""
    for message in messages:
        message.additional_kwargs.pop(store_core._DB_PERSISTED_KEY, None)


class TestToolReturnPersistence:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
    async def test_wrap_then_boundary_keeps_one_row(self, isolated_db, sid, sync):
        middleware = MessagePersistenceMiddleware()
        tool_message = _tool_message("c1", content="file list", msg_id="t1")

        # Given a tool result persisted the moment the handler returned.
        response = await _run_wrap(middleware, _tool_request(sid, "c1"), tool_message, sync=sync)
        assert response is tool_message
        _assert_no_duplicate_rows(sid, 1)

        # When the graph reducer puts that same object into state and the next
        # model boundary scans it again...
        state = _state(
            sid,
            [
                HumanMessage(content="hello", id="h1"),
                _ai_with_calls(["c1"], content="let me check"),
                tool_message,
            ],
        )
        await _run_boundary(middleware, state, sync=sync)

        # Then the tool row is still written exactly once.
        _assert_no_duplicate_rows(sid, 3)
        assert _tool_row_count(sid, "c1") == 1
        assert _watermark_count(sid) == 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
    async def test_parallel_tool_calls_each_get_one_row(self, isolated_db, sid, sync):
        middleware = MessagePersistenceMiddleware()
        tools = [
            _tool_message(f"c{i}", content=f"parallel output {i}", msg_id=f"t{i}") for i in range(3)
        ]

        if sync:
            for index, tool in enumerate(tools):
                assert (
                    middleware.wrap_tool_call(_tool_request(sid, f"c{index}"), _sync_handler(tool))
                    is tool
                )
        else:
            responses = await asyncio.gather(
                *(
                    middleware.awrap_tool_call(
                        _tool_request(sid, f"c{index}"), _async_handler(tool)
                    )
                    for index, tool in enumerate(tools)
                )
            )
            assert all(response is tool for response, tool in zip(responses, tools, strict=True))

        _assert_no_duplicate_rows(sid, 3)
        for index in range(3):
            assert _tool_row_count(sid, f"c{index}") == 1

        # The AI message issuing the parallel calls persists once at the boundary.
        state = _state(
            sid,
            [
                HumanMessage(content="run all", id="h1"),
                _ai_with_calls(["c0", "c1", "c2"], content="parallel calls"),
                *tools,
            ],
        )
        await _run_boundary(middleware, state, sync=sync)
        # 3 tool rows + the human + the multi-call AI message = 5.
        _assert_no_duplicate_rows(sid, 5)

    @pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
    @pytest.mark.asyncio
    async def test_identical_outputs_with_distinct_calls_each_get_one_row(
        self, isolated_db, sid, sync
    ):
        middleware = MessagePersistenceMiddleware()
        first = _tool_message("c0", content="same output", msg_id="t0")
        second = _tool_message("c1", content="same output", msg_id="t1")

        await _run_wrap(middleware, _tool_request(sid, "c0"), first, sync=sync)
        await _run_wrap(middleware, _tool_request(sid, "c1"), second, sync=sync)

        _assert_no_duplicate_rows(sid, 2)
        assert _tool_row_count(sid, "c0") == 1
        assert _tool_row_count(sid, "c1") == 1

    def test_command_and_list_responses_persist_their_tool_messages(self, isolated_db, sid):
        middleware = MessagePersistenceMiddleware()
        nested = _tool_message("c1", content="nested in command", msg_id="t1")
        command = Command(update={"messages": [nested]})
        assert (
            middleware.wrap_tool_call(_tool_request(sid, "c1"), _sync_handler(command)) is command
        )

        listed = _tool_message("c2", content="inside a list", msg_id="t2")
        assert middleware.wrap_tool_call(_tool_request(sid, "c2"), _sync_handler([listed])) == [
            listed
        ]

        navigation = Command(goto="tools")
        assert (
            middleware.wrap_tool_call(_tool_request(sid, "c3"), _sync_handler(navigation))
            is navigation
        )

        _assert_no_duplicate_rows(sid, 2)
        assert _tool_row_count(sid, "c1") == 1
        assert _tool_row_count(sid, "c2") == 1


class TestHitlShortCircuit:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
    async def test_denial_bypasses_wrap_and_lands_at_next_boundary(self, isolated_db, sid, sync):
        hitl = HumanInTheLoop(HITLConfig())
        middleware = MessagePersistenceMiddleware()
        tool_message = _tool_message("c1", content="real output", msg_id="t1")
        hitl.set_interrupt(sid)
        inner_called: list[bool] = []

        def inner_sync(request: ToolCallRequest) -> Any:
            inner_called.append(True)
            return middleware.wrap_tool_call(request, _sync_handler(tool_message))

        async def inner_async(request: ToolCallRequest) -> Any:
            inner_called.append(True)
            return await middleware.awrap_tool_call(request, _async_handler(tool_message))

        # Given HITL's outer wrap short-circuits the call (interrupt path).
        if sync:
            response = hitl.wrap_tool_call(_tool_request(sid, "c1"), inner_sync)
        else:
            response = await hitl.awrap_tool_call(_tool_request(sid, "c1"), inner_async)

        # Then the inner (persisting) layer never ran and nothing was written.
        assert inner_called == []
        assert isinstance(response, ToolMessage)
        assert response.status == "error"
        assert response.tool_call_id == "c1"
        _assert_no_duplicate_rows(sid, 0)

        # When the next model boundary sees the denial in graph state...
        stripped_ai = AIMessage(content="let me run that", id="a1", tool_calls=[])
        await _run_boundary(
            middleware,
            _state(
                sid,
                [
                    HumanMessage(content="hi", id="h1"),
                    stripped_ai,
                    response,
                ],
            ),
            sync=sync,
        )

        # Then the denial is stored exactly once, paired with its AI row.
        _assert_no_duplicate_rows(sid, 3)
        rows = _rows(sid)
        tool_rows = [row for row in rows if row["role"] == "tool"]
        ai_rows = [row for row in rows if row["role"] == "ai"]
        assert len(tool_rows) == 1
        assert tool_rows[0]["tool_status"] == "error"
        assert tool_rows[0]["tool_call_id"] == "c1"
        assert ai_rows[0]["tool_calls"][0]["id"] == "c1"
        # The re-attachment works on a copy — graph state stays untouched.
        assert stripped_ai.tool_calls == []


class TestNoDuplicatesAcrossRestarts:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
    async def test_restart_replay_after_wrap_adds_no_row(self, isolated_db, sid, sync):
        middleware = MessagePersistenceMiddleware()
        # ToolNode builds results WITHOUT an id; the graph reducer assigns one later.
        tool_message = _tool_message("c1", content="file list")
        assert tool_message.id is None

        await _run_wrap(middleware, _tool_request(sid, "c1"), tool_message, sync=sync)
        _assert_no_duplicate_rows(sid, 1)

        # The reducer assigned the id in place, then a restart lost the marker.
        tool_message.id = "t1"
        _clear_persisted_markers([tool_message])

        await _run_boundary(middleware, _state(sid, [tool_message]), sync=sync)
        _assert_no_duplicate_rows(sid, 1)
        assert _tool_row_count(sid, "c1") == 1

        # A second replay at a later boundary still adds nothing.
        await _run_boundary(middleware, _state(sid, [tool_message]), sync=sync)
        _assert_no_duplicate_rows(sid, 1)

    def test_wrap_write_failure_does_not_mark_and_retries(self, isolated_db, sid, monkeypatch):
        middleware = MessagePersistenceMiddleware()
        tool_message = _tool_message("c1", content="file list", msg_id="t1")
        real_add = mp_core.add_messages_sync
        attempts: list[str] = []

        def flaky_add(session_id: str, messages: list) -> None:
            attempts.append(session_id)
            if len(attempts) == 1:
                raise RuntimeError("simulated store failure")
            real_add(session_id, messages)

        monkeypatch.setattr(mp_core, "add_messages_sync", flaky_add)

        # Given the wrap write fails...
        assert (
            middleware.wrap_tool_call(_tool_request(sid, "c1"), _sync_handler(tool_message))
            is tool_message
        )
        _assert_no_duplicate_rows(sid, 0)
        assert _watermark_count(sid) == 0

        # When the next boundary retries, the message is stored exactly once.
        middleware.after_model(_state(sid, [tool_message]), None)
        _assert_no_duplicate_rows(sid, 1)
        assert _watermark_count(sid) == 1
        assert len(attempts) == 2

    @pytest.mark.asyncio
    async def test_multi_round_tool_turns_do_not_accumulate_rows(self, isolated_db, sid):
        middleware = MessagePersistenceMiddleware()
        session_messages: list = []

        for index in range(3):
            human = HumanMessage(content=f"question {index}", id=f"h{index}")
            ai_call = _ai_with_calls([f"c{index}"], content=f"let me check {index}")
            tool = _tool_message(f"c{index}", content=f"round output {index}", msg_id=f"t{index}")
            ai_done = AIMessage(content=f"final answer {index}", id=f"d{index}")

            session_messages += [human, ai_call]
            await _run_boundary(middleware, _state(sid, session_messages), sync=False)
            assert (
                await middleware.awrap_tool_call(
                    _tool_request(sid, f"c{index}"), _async_handler(tool)
                )
                is tool
            )
            session_messages += [tool, ai_done]
            await _run_boundary(middleware, _state(sid, session_messages), sync=False)

        _assert_no_duplicate_rows(sid, 12)
        assert _watermark_count(sid) == 12
        for index in range(3):
            assert _tool_row_count(sid, f"c{index}") == 1
