"""Boundary cases for the context-governance stack.

These complement the happy-path suites: partially-oversized parallel tool
batches, human + tool eviction markers coexisting in one session, the
read_file slice passing through the model-view rewrite untouched, the
TaskIntent steering message as the true last message, literal ``read_file``
hints in evicted originals, summary pairs next to eviction tags, and the
evict -> tag -> clip stack keeping a single persistence row.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from agent.middlewares.context_eviction import ContextEvictionMiddleware
from agent.middlewares.message_persistence import MessagePersistenceMiddleware
from agent.middlewares.summarization.core import _filter_summary_messages
from agent.middlewares.task_intent.core import _TASK_STEERING_PROMPT
from config.features import SUMMARIZATION, TOOL_RESULT_EVICTION
from context_engine.store import core as store_core
from context_engine.store.core import get_history_by_turn_page
from pub.func.message.eviction import (
    _READ_FILE_SLICE_NOTICE,
    EVICTED_TO_KEY,
    load_evicted,
    slice_read_file_result,
)
from pub.func.message.overflow_clip import CLIP_MARKER, clip_overflow_tail

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]

THRESHOLD = TOOL_RESULT_EVICTION["evict_threshold_chars"]
HUMAN_THRESHOLD = TOOL_RESULT_EVICTION["human_evict_threshold_chars"]
READ_FILE_CONTENT = '{"content": "' + ("y" * 60 + "\\n") * 300 + '", "total_lines": 300}'


class _StubModel:
    model_name = "stub"


def _big_content(extra: int = 5_000) -> str:
    total = THRESHOLD + extra
    return ("x" * 99 + "\n") * (total // 100) + "x" * (total % 100)


def _huge_human_text(extra: int = 1) -> str:
    total = HUMAN_THRESHOLD + extra
    return ("u" * 99 + "\n") * (total // 100) + "u" * (total % 100)


def _huge_human_with_hint(hint: str) -> str:
    tail = "{path} and {notice} literal"
    filler = "u" * 99
    count = max(0, (HUMAN_THRESHOLD + 1 - len(hint) - len(tail)) // 100 + 1)
    return "\n".join([hint, *[filler] * count, tail])


def _big_tool(tool_call_id: str = "c1", msg_id: str | None = "t1") -> ToolMessage:
    return ToolMessage(
        content=_big_content(), name="terminal", tool_call_id=tool_call_id, id=msg_id
    )


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


def _state(session_id: str, messages: list[Any]) -> dict[str, Any]:
    return {"session_id": session_id, "messages": messages}


def _model_request(messages: list[Any], session_id: str) -> ModelRequest:
    return ModelRequest(
        model=_StubModel(),  # type: ignore[arg-type]
        messages=list(messages),
        state=_state(session_id, list(messages)),
    )


def _sync_handler(value: Any):
    def handler(_request: ToolCallRequest) -> Any:
        return value

    return handler


def _recording_handler() -> tuple[dict[str, Any], Any]:
    captured: dict[str, Any] = {}

    def handler(inner: ModelRequest) -> AIMessage:
        captured["request"] = inner
        return AIMessage(content="ok")

    return captured, handler


def _tagged(session_id: str, middleware: ContextEvictionMiddleware) -> HumanMessage:
    update = middleware.before_model(_state(session_id, [_huge_human()]), None)
    assert update is not None
    return update["messages"][0]


def _huge_human(**kwargs: Any) -> HumanMessage:
    return HumanMessage(content=_huge_human_text(), id="h1", **kwargs)


def _evicted_path(preview: str) -> Path:
    match = re.search(r"\[evicted to: (.*?)\]", preview)
    assert match is not None, f"preview carries no eviction path: {preview[:120]}"
    return Path(match.group(1))


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


def _tool_rows(session_id: str) -> list[dict]:
    return [
        row
        for row in get_history_by_turn_page(session_id, turn_page_size=1000)
        if row["role"] == "tool"
    ]


def _watermark_count(session_id: str) -> int:
    row = store_core._db.execute(
        "SELECT COUNT(*) FROM persisted_message_ids WHERE session_id = ?", (session_id,)
    ).fetchone()
    return int(row[0])


class TestMixedToolEviction:
    def test_partial_parallel_batch_evicts_only_the_oversized_result(
        self, isolated_db, isolated_sessions, sid
    ):
        eviction = ContextEvictionMiddleware()
        big = _big_tool("c1", "t1")
        small = ToolMessage(content="ok", name="terminal", tool_call_id="c2", id="t2")

        response = eviction.wrap_tool_call(_request(sid, "c1"), _sync_handler([big, small]))

        assert isinstance(response, list)
        assert response[0].id == "t1"
        assert response[0].content.startswith("[evicted to: ")
        assert response[1] is small
        files = list((isolated_sessions / sid / "evicted").iterdir())
        assert len(files) == 1

    def test_command_batch_evicts_only_the_oversized_result(
        self, isolated_db, isolated_sessions, sid
    ):
        eviction = ContextEvictionMiddleware()
        big = _big_tool("c1", "t1")
        small = ToolMessage(content="ok", name="terminal", tool_call_id="c2", id="t2")

        response = eviction.wrap_tool_call(
            _request(sid, "c1"), _sync_handler(Command(update={"messages": [big, small]}))
        )

        assert isinstance(response, Command)
        rewritten = response.update["messages"]
        assert rewritten[0].id == "t1"
        assert rewritten[0].content.startswith("[evicted to: ")
        assert rewritten[1] is small


class TestHumanToolCoexistence:
    def test_human_and_tool_eviction_coexist_in_one_session(
        self, isolated_db, isolated_sessions, sid
    ):
        eviction = ContextEvictionMiddleware()
        tagged = _tagged(sid, eviction)

        tool_response = eviction.wrap_tool_call(
            _request(sid, "c1"), _sync_handler(_big_tool("c1", "t1"))
        )

        files = sorted(p.suffix for p in (isolated_sessions / sid / "evicted").iterdir())
        assert files == [".md", ".txt"]
        assert tagged.additional_kwargs[EVICTED_TO_KEY]
        assert tool_response.content.startswith("[evicted to: ")
        assert EVICTED_TO_KEY not in tool_response.additional_kwargs

    def test_human_view_replacement_leaves_tool_messages_untouched(
        self, isolated_db, isolated_sessions, sid
    ):
        eviction = ContextEvictionMiddleware()
        tagged = _tagged(sid, eviction)
        sliced = slice_read_file_result(
            ToolMessage(content=READ_FILE_CONTENT, name="read_file", tool_call_id="r1", id="t2")
        )
        assert sliced.content.endswith(_READ_FILE_SLICE_NOTICE)
        captured, handler = _recording_handler()

        eviction.wrap_model_call(_model_request([tagged, sliced], sid), handler)

        seen = captured["request"].messages
        assert seen[1] is sliced
        assert seen[1].content.endswith(_READ_FILE_SLICE_NOTICE)
        assert seen[0] is not tagged
        assert seen[0].id == tagged.id
        assert seen[0].content.startswith("[evicted to: ")


class TestTaskIntentSteeringBoundary:
    def test_injected_steering_last_skips_the_earlier_oversized_human(
        self, isolated_db, isolated_sessions, sid
    ):
        eviction = ContextEvictionMiddleware()
        oversized = HumanMessage(content=_huge_human_text(), id="h1")
        steering = HumanMessage(content=_TASK_STEERING_PROMPT, id="h2")
        assert len(_TASK_STEERING_PROMPT) > 100

        assert eviction.before_model(_state(sid, [oversized, steering]), None) is None
        assert not (isolated_sessions / sid).exists()

    def test_oversized_true_last_message_is_the_one_evicted(
        self, isolated_db, isolated_sessions, sid
    ):
        eviction = ContextEvictionMiddleware()
        steering = HumanMessage(content=_TASK_STEERING_PROMPT, id="h1")
        oversized = HumanMessage(content=_huge_human_text(), id="h2")

        update = eviction.before_model(_state(sid, [steering, oversized]), None)

        assert update is not None
        tagged = update["messages"][0]
        assert tagged.id == "h2"
        assert tagged.content == oversized.content
        assert load_evicted(_evicted_path_of(tagged)) == oversized.content


class TestPreviewNestingBoundary:
    def test_original_read_file_hint_survives_verbatim(self, isolated_db, isolated_sessions, sid):
        eviction = ContextEvictionMiddleware()
        hint = "read_file(file_path='/original/path.md', offset=0, limit=100)"
        original = HumanMessage(content=_huge_human_with_hint(hint), id="h1")
        update = eviction.before_model(_state(sid, [original]), None)
        assert update is not None
        tagged = update["messages"][0]
        path = _evicted_path_of(tagged)
        assert load_evicted(path) == original.content

        captured, handler = _recording_handler()
        eviction.wrap_model_call(_model_request([tagged], sid), handler)

        preview = captured["request"].messages[0].content
        assert isinstance(preview, str)
        assert preview.startswith("[evicted to: ")
        assert hint in preview
        assert "{path}" in preview
        assert "{notice}" in preview
        assert str(path) in preview


class TestSummaryAndEvictionCoexist:
    def test_summary_pairs_are_dropped_while_an_eviction_tag_survives(
        self, isolated_db, isolated_sessions, sid
    ):
        eviction = ContextEvictionMiddleware()
        tagged = _tagged(sid, eviction)
        tag = {"lc_source": "summarization"}
        question = HumanMessage(content="What did we do so far?", additional_kwargs=tag)
        summary = AIMessage(content="previous summary", additional_kwargs=tag)
        normal = AIMessage(content="keep me")

        result = _filter_summary_messages([question, summary, tagged, normal])

        assert result == [tagged, normal]
        assert result[0].additional_kwargs[EVICTED_TO_KEY] == str(_evicted_path_of(tagged))


class TestPersistenceWatermarkStacked:
    def test_evict_tag_clip_keeps_a_single_row_across_boundaries(
        self, isolated_db, isolated_sessions, sid, monkeypatch
    ):
        monkeypatch.setitem(SUMMARIZATION, "overflow_clip_min_keep", 2)
        eviction = ContextEvictionMiddleware()
        persistence = MessagePersistenceMiddleware()
        original = _big_tool("c1", "t1")
        full_text = original.content

        preview = eviction.wrap_tool_call(
            _request(sid, "c1"),
            lambda inner: persistence.wrap_tool_call(inner, _sync_handler(original)),
        )
        assert preview.content.startswith("[evicted to: ")
        assert _tool_row_count(sid, "c1") == 1

        state = _state(
            sid,
            [
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
                preview,
            ],
        )
        clipped = clip_overflow_tail(state["messages"], target_tokens=0)
        assert clipped is not None
        assert CLIP_MARKER in clipped[-1].content
        assert clipped[-1].id == "t1"

        persistence.after_model(_state(sid, clipped), None)
        persistence.after_model(_state(sid, clipped), None)

        assert _row_count(sid) == 3
        assert _tool_row_count(sid, "c1") == 1
        assert _tool_rows(sid)[0]["content"] == full_text
        assert _watermark_count(sid) == 3


def _evicted_path_of(message: HumanMessage) -> Path:
    return Path(message.additional_kwargs[EVICTED_TO_KEY])
