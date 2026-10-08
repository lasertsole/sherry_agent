"""Human-message context eviction (P1-9): tagging, view truncation, interactions.

State keeps the full text — ``before_model`` only adds
``additional_kwargs["lc_evicted_to"]`` to an in-place ``model_copy`` — so:

- the ``add_messages`` reducer replaces the message by id, never rewrites the
  list (no ``REMOVE_ALL_MESSAGES`` sentinel);
- ``MessagePersistenceMiddleware`` archives the full text and the watermark is
  unaffected (the id does not change);
- the eviction file round-trips byte-identically and is self-healed from the
  state text when missing;
- only the model view is replaced by the path + ``read_file`` preview, and
  media blocks survive.

This is deliberately the opposite split from the tool path (state = preview).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import add_messages

from agent.middlewares.context_eviction import ContextEvictionMiddleware
from agent.middlewares.message_persistence import MessagePersistenceMiddleware
from agent.middlewares.summarization.core import _filter_summary_messages
from config.features import TOOL_RESULT_EVICTION
from context_engine import search_messages
from context_engine.store import core as store_core
from context_engine.store.core import get_history_by_turn_page
from pub.func import sanitize_tool_use_result_pairing
from pub.func.message.eviction import EVICTED_TO_KEY, load_evicted
from pub.func.message.overflow_clip import clip_overflow_tail

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]

THRESHOLD = TOOL_RESULT_EVICTION["human_evict_threshold_chars"]
IMAGE = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


class _StubModel:
    model_name = "stub"


def _fill(total: int) -> str:
    """Multiline content of exactly ``total`` characters (99-char lines)."""
    return ("u" * 99 + "\n") * (total // 100) + "u" * (total % 100)


def _big_human(
    *, msg_id: str | None = "h1", extra: int = 1, blocks: list[Any] | None = None
) -> HumanMessage:
    text = _fill(THRESHOLD + extra)
    if blocks:
        return HumanMessage(content=[{"type": "text", "text": text}, *blocks], id=msg_id)
    return HumanMessage(content=text, id=msg_id)


def _state(session_id: str, messages: list[Any]) -> dict[str, Any]:
    return {"session_id": session_id, "messages": messages}


def _request(messages: list[Any], session_id: str) -> ModelRequest:
    return ModelRequest(
        model=_StubModel(),  # type: ignore[arg-type]
        messages=list(messages),
        state=_state(session_id, list(messages)),
    )


def _recording_handler() -> tuple[dict[str, Any], Any]:
    captured: dict[str, Any] = {}

    def handler(inner: ModelRequest) -> AIMessage:
        captured["request"] = inner
        return AIMessage(content="ok")

    return captured, handler


def _async_recording_handler() -> tuple[dict[str, Any], Any]:
    captured: dict[str, Any] = {}

    async def handler(inner: ModelRequest) -> AIMessage:
        captured["request"] = inner
        return AIMessage(content="ok")

    return captured, handler


def _tagged(session_id: str, middleware: ContextEvictionMiddleware, **kwargs: Any) -> HumanMessage:
    update = middleware.before_model(_state(session_id, [_big_human(**kwargs)]), None)
    assert update is not None
    return update["messages"][0]


def _evicted_path(message: HumanMessage) -> Path:
    return Path(message.additional_kwargs[EVICTED_TO_KEY])


def _row_count(session_id: str) -> int:
    row = store_core._db.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
    ).fetchone()
    return int(row[0])


def _human_rows(session_id: str) -> list[dict]:
    return [
        row
        for row in get_history_by_turn_page(session_id, turn_page_size=1000)
        if row["role"] == "human"
    ]


class TestTaggingGate:
    def test_at_threshold_is_not_tagged(self, isolated_db, isolated_sessions, sid):
        middleware = ContextEvictionMiddleware()

        assert middleware.before_model(_state(sid, [_big_human(extra=0)]), None) is None
        assert not (isolated_sessions / sid).exists()

    def test_one_over_is_tagged_and_returns_a_partial_update(
        self, isolated_db, isolated_sessions, sid
    ):
        middleware = ContextEvictionMiddleware()
        original = _big_human()

        update = middleware.before_model(_state(sid, [original]), None)

        assert update is not None and set(update) == {"messages"}
        tagged = update["messages"][0]
        assert tagged is not original
        assert tagged.id == "h1"
        # State semantics: content unchanged, only additional_kwargs gains the tag.
        assert tagged.content == original.content
        assert _evicted_path(tagged).parent == isolated_sessions / sid / "evicted"
        assert load_evicted(_evicted_path(tagged)) == original.content
        # The input state object was never mutated.
        assert EVICTED_TO_KEY not in original.additional_kwargs

    def test_non_last_human_is_not_tagged(self, isolated_db, isolated_sessions, sid):
        middleware = ContextEvictionMiddleware()
        messages = [_big_human(), AIMessage(content="ack")]

        assert middleware.before_model(_state(sid, messages), None) is None

    def test_empty_and_malformed_states_return_none(self, isolated_db, isolated_sessions, sid):
        middleware = ContextEvictionMiddleware()

        assert middleware.before_model(_state(sid, []), None) is None
        assert middleware.before_model({"session_id": sid}, None) is None
        assert middleware.before_model("not-a-state", None) is None

    def test_disabled_flag_skips_tagging(self, isolated_db, isolated_sessions, sid, monkeypatch):
        monkeypatch.setitem(TOOL_RESULT_EVICTION, "human_evict_enabled", False)
        middleware = ContextEvictionMiddleware()

        assert middleware.before_model(_state(sid, [_big_human()]), None) is None
        assert not (isolated_sessions / sid).exists()

    def test_already_tagged_is_idempotent(self, isolated_db, isolated_sessions, sid):
        middleware = ContextEvictionMiddleware()
        tagged = _tagged(sid, middleware)
        evicted_dir = _evicted_path(tagged).parent

        update = middleware.before_model(_state(sid, [tagged]), None)

        assert update is None
        assert len(list(evicted_dir.iterdir())) == 1

    def test_single_giant_line_is_not_tagged(self, isolated_db, isolated_sessions, sid):
        middleware = ContextEvictionMiddleware()
        message = HumanMessage(content="u" * (THRESHOLD + 1), id="h1")

        assert middleware.before_model(_state(sid, [message]), None) is None
        assert not (isolated_sessions / sid).exists()

    def test_missing_session_id_returns_none(self, isolated_db, isolated_sessions):
        middleware = ContextEvictionMiddleware()
        messages = [_big_human()]

        assert middleware.before_model({"messages": messages}, None) is None

    @pytest.mark.parametrize("bad", ["..", "a/b", "a\\b", "../escape"])
    def test_unsafe_session_segment_skips_every_write(self, isolated_db, isolated_sessions, bad):
        middleware = ContextEvictionMiddleware()

        assert middleware.before_model(_state(bad, [_big_human()]), None) is None
        assert not isolated_sessions.exists() or not any(isolated_sessions.rglob("*"))

    @pytest.mark.asyncio
    async def test_async_before_model_tags_the_last_human(
        self, isolated_db, isolated_sessions, sid
    ):
        middleware = ContextEvictionMiddleware()
        original = _big_human()

        update = await middleware.abefore_model(_state(sid, [original]), None)

        assert update is not None
        assert update["messages"][0].content == original.content
        assert load_evicted(_evicted_path(update["messages"][0])) == original.content


class TestReducerInPlace:
    def test_add_messages_replaces_by_id_without_a_list_rewrite(
        self, isolated_db, isolated_sessions, sid
    ):
        middleware = ContextEvictionMiddleware()
        original = _big_human()
        update = middleware.before_model(_state(sid, [original]), None)
        assert update is not None

        merged = add_messages([original], update["messages"])

        assert len(merged) == 1
        assert merged[0].id == "h1"
        assert merged[0].content == original.content
        assert merged[0].additional_kwargs[EVICTED_TO_KEY] == str(
            _evicted_path(update["messages"][0])
        )

    def test_earlier_messages_survive_in_order(self, isolated_db, isolated_sessions, sid):
        middleware = ContextEvictionMiddleware()
        earlier = AIMessage(content="previous", id="a0")
        original = _big_human()
        update = middleware.before_model(_state(sid, [earlier, original]), None)
        assert update is not None and len(update["messages"]) == 1
        assert not any(isinstance(message, RemoveMessage) for message in update["messages"])

        merged = add_messages([earlier, original], update["messages"])

        assert [message.id for message in merged] == ["a0", "h1"]
        assert merged[0] is earlier
        assert merged[1].content == original.content


class TestThreeStateAndPersistence:
    def test_state_full_mesmemory_full_disk_full(self, isolated_db, isolated_sessions, sid):
        middleware = ContextEvictionMiddleware()
        persistence = MessagePersistenceMiddleware()
        original = _big_human()
        tagged = _tagged(sid, middleware)

        persistence.after_model(_state(sid, [tagged]), None)

        # state: full text + tag.
        assert tagged.content == original.content
        assert tagged.additional_kwargs[EVICTED_TO_KEY]
        # MesMemory: the full text (the tag does not filter persistence).
        rows = _human_rows(sid)
        assert len(rows) == 1
        assert rows[0]["content"] == original.content
        # Disk: byte-identical to the state text.
        assert load_evicted(_evicted_path(tagged)) == original.content

    def test_second_boundary_writes_no_second_row(self, isolated_db, isolated_sessions, sid):
        middleware = ContextEvictionMiddleware()
        persistence = MessagePersistenceMiddleware()
        tagged = _tagged(sid, middleware)
        state = _state(sid, [tagged])

        persistence.after_model(state, None)
        persistence.after_model(state, None)

        assert _row_count(sid) == 1

    def test_message_search_recalls_the_full_text(
        self, isolated_db, isolated_sessions, sid, monkeypatch
    ):
        from context_engine import core as context_core

        monkeypatch.setattr(context_core, "_db", store_core._db)
        middleware = ContextEvictionMiddleware()
        persistence = MessagePersistenceMiddleware()
        needle = "NEEDLETOKEN"
        text = needle + " " + _fill(THRESHOLD + 1 - len(needle) - 1)
        original = HumanMessage(content=text, id="h1")
        update = middleware.before_model(_state(sid, [original]), None)
        assert update is not None
        persistence.after_model(_state(sid, [update["messages"][0]]), None)

        matches = search_messages(needle, sid)

        assert matches
        assert any(needle in (match.get("snippet") or "") for match in matches)


class TestModelView:
    @pytest.mark.parametrize("sync", [True, False], ids=["sync", "async"])
    @pytest.mark.asyncio
    async def test_view_is_replaced_with_path_and_read_file_hint(
        self, isolated_db, isolated_sessions, sid, sync
    ):
        middleware = ContextEvictionMiddleware()
        tagged = _tagged(sid, middleware)
        request = _request([tagged], sid)
        captured, handler = _recording_handler() if sync else _async_recording_handler()

        if sync:
            result = middleware.wrap_model_call(request, handler)
        else:
            result = await middleware.awrap_model_call(request, handler)

        assert isinstance(result, AIMessage)
        seen = captured["request"].messages[0]
        assert seen.id == "h1"
        assert isinstance(seen.content, str)
        assert seen.content.startswith("[evicted to: ")
        assert "read_file(file_path=" in seen.content and "offset=0, limit=100" in seen.content
        assert str(_evicted_path(tagged)) in seen.content
        assert seen.additional_kwargs[EVICTED_TO_KEY] == str(_evicted_path(tagged))
        # The original request stays untouched: state/checkpoint keep the full text.
        assert request.messages[0].content == tagged.content

    def test_media_blocks_survive_in_the_view(self, isolated_db, isolated_sessions, sid):
        middleware = ContextEvictionMiddleware()
        tagged = _tagged(sid, middleware, blocks=[IMAGE])
        request = _request([tagged], sid)
        captured, handler = _recording_handler()

        middleware.wrap_model_call(request, handler)

        view = captured["request"].messages[0]
        assert isinstance(view.content, list)
        assert view.content[0]["type"] == "text"
        assert view.content[0]["text"].startswith("[evicted to: ")
        assert view.content[1] == IMAGE

    def test_untagged_messages_pass_through_untouched(self, isolated_db, isolated_sessions, sid):
        middleware = ContextEvictionMiddleware()
        request = _request([HumanMessage(content="small", id="h1")], sid)
        captured, handler = _recording_handler()

        middleware.wrap_model_call(request, handler)

        assert captured["request"] is request

    def test_disabled_flag_leaves_the_view_untouched(
        self, isolated_db, isolated_sessions, sid, monkeypatch
    ):
        tagged = _tagged(sid, ContextEvictionMiddleware())
        monkeypatch.setitem(TOOL_RESULT_EVICTION, "human_evict_enabled", False)
        middleware = ContextEvictionMiddleware()
        request = _request([tagged], sid)
        captured, handler = _recording_handler()

        middleware.wrap_model_call(request, handler)

        assert captured["request"] is request

    def test_self_heal_rewrites_a_missing_file_from_state(
        self, isolated_db, isolated_sessions, sid
    ):
        middleware = ContextEvictionMiddleware()
        original = _big_human()
        tagged = _tagged(sid, middleware)
        path = _evicted_path(tagged)
        path.unlink()

        captured, handler = _recording_handler()
        middleware.wrap_model_call(_request([tagged], sid), handler)

        assert path.read_text(encoding="utf-8") == original.content
        assert captured["request"].messages[0].content.startswith("[evicted to: ")

    def test_self_heal_refuses_a_path_outside_the_session(
        self, isolated_db, isolated_sessions, sid, tmp_path
    ):
        middleware = ContextEvictionMiddleware()
        foreign = tmp_path / "foreign.md"
        tagged = _big_human().model_copy(
            update={"additional_kwargs": {EVICTED_TO_KEY: str(foreign)}}
        )

        captured, handler = _recording_handler()
        middleware.wrap_model_call(_request([tagged], sid), handler)

        assert not foreign.exists()
        assert captured["request"].messages[0].content.startswith("[evicted to: ")


class TestInteractions:
    def test_tail_clip_never_touches_a_human_message(self, isolated_db, isolated_sessions, sid):
        middleware = ContextEvictionMiddleware()
        tagged = _tagged(sid, middleware)
        ai = AIMessage(content="", tool_calls=[{"name": "terminal", "args": {}, "id": "c1"}])
        tools = [
            ToolMessage(content="z" * 10_000, name="terminal", tool_call_id=f"c{n}")
            for n in range(1, 5)
        ]

        clipped = clip_overflow_tail([tagged, ai, *tools], target_tokens=0)

        assert clipped is not None
        assert clipped[0] is tagged
        assert tagged.additional_kwargs[EVICTED_TO_KEY]
        assert any(new is not old for new, old in zip(clipped[2:], tools, strict=True))

    def test_summary_filter_keeps_a_tagged_human(self, isolated_db, isolated_sessions, sid):
        middleware = ContextEvictionMiddleware()
        tagged = _tagged(sid, middleware)

        assert _filter_summary_messages([tagged]) == [tagged]

    def test_pairing_sanitizer_keeps_a_tagged_human(self, isolated_db, isolated_sessions, sid):
        middleware = ContextEvictionMiddleware()
        tagged = _tagged(sid, middleware)
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "terminal", "args": {}, "id": "c1", "type": "tool_call"}],
        )
        tool = ToolMessage(content="r", name="terminal", tool_call_id="c1")

        repaired = sanitize_tool_use_result_pairing([tagged, ai, tool])

        assert repaired[0] is tagged
        assert repaired[0].additional_kwargs[EVICTED_TO_KEY] == str(_evicted_path(tagged))
