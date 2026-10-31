"""Branched side chats: fork at any parent turn (``fork_turn_num``).

A branch reuses the side-chat fork (checkpoint transcript + settings + cascade
purge) and only parameterizes its fork point:

- ``_history_up_to_turn`` cuts the parent transcript after turn N's last
  completed answer, counting USER turns (reminder blocks / notices excluded so
  the count matches the client's ``turn_num``);
- ``fork_turn_num is None`` keeps the legacy side-chat cut byte-for-byte;
- the fork point survives in the link table (an old DB file gains the column
  on connect; old rows read as ``None`` = legacy default);
- creation validates it and answers it back, dedupe included.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from server.DAO import side_chat_store
from server.service import side_chat_service
from server.trigger.http import side_chat as side_chat_http

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(side_chat_store, "_DB_PATH", tmp_path / "side_chats.db")


def _turn(question: str, answer: str) -> list:
    return [HumanMessage(content=question), AIMessage(content=answer)]


def _transcript(*turns: tuple[str, str]) -> list:
    messages: list = []
    for question, answer in turns:
        messages.extend(_turn(question, answer))
    return messages


class TestHistoryUpToTurn:
    def test_none_keeps_the_legacy_last_answer_cut(self) -> None:
        messages = _transcript(("q1", "a1"), ("q2", "a2"))

        assert side_chat_service._history_up_to_turn(messages, None) == messages

    def test_cuts_after_the_selected_turns_answer(self) -> None:
        messages = _transcript(("q1", "a1"), ("q2", "a2"), ("q3", "a3"))

        cut = side_chat_service._history_up_to_turn(messages, 2)

        assert [message.content for message in cut] == ["q1", "a1", "q2", "a2"]

    def test_an_internal_human_message_is_not_a_turn_boundary(self) -> None:
        messages = [
            HumanMessage(content="q1"),
            AIMessage(content="a1"),
            HumanMessage(
                content="<system-reminder>notice</system-reminder>",
                metadata={"internal": True, "provenance": "reminder_catalog"},
            ),
            HumanMessage(content="q2"),
            AIMessage(content="a2"),
            HumanMessage(content="q3"),
            AIMessage(content="a3"),
        ]

        cut = side_chat_service._history_up_to_turn(messages, 2)

        assert [message.content for message in cut][-2:] == ["q2", "a2"]

    def test_a_compacted_turn_answers_empty(self) -> None:
        assert side_chat_service._history_up_to_turn(_transcript(("q1", "a1")), 5) == []

    def test_an_agentic_turn_cuts_at_its_final_answer(self) -> None:
        messages = [
            HumanMessage(content="q1"),
            AIMessage(content="a1"),
            HumanMessage(content="q2"),
            AIMessage(content="", tool_calls=[{"name": "terminal", "args": {}, "id": "c1"}]),
            ToolMessage(content="tool result", tool_call_id="c1"),
            AIMessage(content="final a2"),
            HumanMessage(content="q3"),
            AIMessage(content="a3"),
        ]

        cut = side_chat_service._history_up_to_turn(messages, 2)

        assert cut[-1].content == "final a2"
        assert len(cut) == 6

    def test_a_mid_tool_turn_is_cut_before_its_answer_work(self) -> None:
        messages = [
            HumanMessage(content="q1"),
            AIMessage(content="a1"),
            HumanMessage(content="q2"),
            AIMessage(content="", tool_calls=[{"name": "terminal", "args": {}, "id": "c1"}]),
            ToolMessage(content="tool result", tool_call_id="c1"),
            HumanMessage(content="q3"),
            AIMessage(content="a3"),
        ]

        cut = side_chat_service._history_up_to_turn(messages, 2)

        # The dangling tool call is NOT replayed; the child starts after q1's answer.
        assert [message.content for message in cut] == ["q1", "a1"]


@pytest.mark.asyncio
async def test_a_branch_fork_cuts_at_its_turn(monkeypatch) -> None:
    side_chat_store.create_side_chat("child-1", "parent-1", fork_mode=True, fork_turn_num=1)
    import agent.state_port as state_port
    import context_engine.store.core as store_core

    monkeypatch.setattr(store_core, "session_has_messages", lambda _sid: False)

    async def fake_read_messages(_parent: str):
        return _transcript(("q1", "a1"), ("q2", "a2"), ("q3", "a3"))

    monkeypatch.setattr(state_port, "read_messages", fake_read_messages)

    inherited = await side_chat_service.get_checkpoint_fork_for_turn("child-1")

    assert inherited is not None
    assert [message.content for message in inherited[:-1]] == ["q1", "a1"]
    assert "complete fork" in str(inherited[-1].content)


@pytest.mark.asyncio
async def test_a_legacy_link_without_a_turn_keeps_the_last_answer(monkeypatch) -> None:
    side_chat_store.create_side_chat("child-1", "parent-1", fork_mode=True)
    import agent.state_port as state_port
    import context_engine.store.core as store_core

    monkeypatch.setattr(store_core, "session_has_messages", lambda _sid: False)

    async def fake_read_messages(_parent: str):
        return _transcript(("q1", "a1"), ("q2", "a2"))

    monkeypatch.setattr(state_port, "read_messages", fake_read_messages)

    inherited = await side_chat_service.get_checkpoint_fork_for_turn("child-1")

    assert inherited is not None
    assert [message.content for message in inherited[:-1]] == ["q1", "a1", "q2", "a2"]


class TestForkTurnInTheLinkTable:
    def test_the_fork_point_round_trips(self) -> None:
        side_chat_store.create_side_chat("child-1", "parent-1", fork_mode=True, fork_turn_num=3)

        row = side_chat_store.get_side_chat("child-1")

        assert row is not None and row["fork_turn_num"] == 3
        assert side_chat_store.all_side_chats()[0]["fork_turn_num"] == 3

    def test_an_old_file_gains_the_columns_and_reads_as_none(self, tmp_path, monkeypatch) -> None:
        legacy = tmp_path / "legacy_side_chats.db"
        conn = sqlite3.connect(legacy)
        # The first release had NEITHER client_request_id nor fork_turn_num; a
        # statement naming a missing column used to 500 every side-chat request.
        conn.execute(
            "CREATE TABLE side_chats ("
            "child_session_id TEXT PRIMARY KEY, parent_session_id TEXT NOT NULL, "
            "context_text TEXT NOT NULL DEFAULT '', fork_mode INTEGER NOT NULL DEFAULT 0, "
            "created_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO side_chats (child_session_id, parent_session_id, created_at) "
            "VALUES ('old-child', 'parent-1', 1)"
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(side_chat_store, "_DB_PATH", legacy)

        row = side_chat_store.get_side_chat("old-child")

        assert row is not None and row["fork_turn_num"] is None
        # Both columns were added: the INSERT and the idempotency lookup (which
        # FILTERS on client_request_id) work again on the upgraded file.
        side_chat_store.create_side_chat(
            "new-child", "parent-1", fork_mode=True, fork_turn_num=2, client_request_id="req-1"
        )
        assert side_chat_store.get_side_chat("new-child")["fork_turn_num"] == 2
        assert (
            side_chat_store.find_by_request("parent-1", "req-1")["child_session_id"] == "new-child"
        )


class TestCreateValidates:
    @pytest.mark.parametrize("bad", [0, -1, "abc", True])
    @pytest.mark.asyncio
    async def test_an_invalid_fork_turn_is_refused(self, bad) -> None:
        with pytest.raises(ValueError, match="invalid fork_turn_num"):
            await side_chat_service.create_side_chat("parent-1", None, bad)

    @pytest.mark.asyncio
    async def test_the_batch_turn_is_stored_as_the_user_turn_ordinal(self, monkeypatch) -> None:
        """The client sends the AI row's per-BATCH turn; the cut counts USER turns.

        A live session persists user=1, tool=2, answer=3 …, so forking the answer
        row (turn 3) must store ordinal 1 — the same translation the message edit
        performs. Storing 3 would miss the checkpoint and silently fall back to
        the full text snapshot (live-verified against the dev backend).
        """
        monkeypatch.setattr(side_chat_service, "build_parent_context", _async_value("context"))
        monkeypatch.setattr(
            "context_engine.store.core.find_user_turn_cut",
            lambda _sid, turn, _row=None: (1, 1) if turn == 3 else None,
            raising=False,
        )

        result = await side_chat_service.create_side_chat("parent-1", "req-1", 3)

        assert result["fork_turn_num"] == 1
        row = side_chat_store.get_side_chat(result["session_id"])
        assert row is not None and row["fork_turn_num"] == 1

    @pytest.mark.asyncio
    async def test_an_unpersisted_fork_turn_falls_back_to_the_last_answer(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(side_chat_service, "build_parent_context", _async_value("context"))
        monkeypatch.setattr(
            "context_engine.store.core.find_user_turn_cut",
            lambda *_args, **_kwargs: None,
            raising=False,
        )

        result = await side_chat_service.create_side_chat("parent-1", "req-2", 9)

        # A compacted/unpersisted row degrades to the side-chat cut, not a 500.
        assert result["fork_turn_num"] is None
        assert side_chat_store.get_side_chat(result["session_id"])["fork_turn_num"] is None

    @pytest.mark.asyncio
    async def test_a_deduped_creation_answers_the_stored_turn(self, monkeypatch) -> None:
        monkeypatch.setattr(side_chat_service, "build_parent_context", _async_value("context"))
        monkeypatch.setattr(
            "context_engine.store.core.find_user_turn_cut",
            lambda _sid, turn, _row=None: (turn, turn),
            raising=False,
        )
        first = await side_chat_service.create_side_chat("parent-1", "req-1", 2)

        second = await side_chat_service.create_side_chat("parent-1", "req-1", 2)

        assert second["session_id"] == first["session_id"]
        assert second["deduplicated"] is True
        assert second["fork_turn_num"] == 2

    @pytest.mark.asyncio
    async def test_links_list_the_branch_points(self, monkeypatch) -> None:
        monkeypatch.setattr(side_chat_service, "build_parent_context", _async_value("context"))
        monkeypatch.setattr(
            "context_engine.store.core.find_user_turn_cut",
            lambda _sid, turn, _row=None: (3, 4),
            raising=False,
        )
        await side_chat_service.create_side_chat("parent-1", None, 4)

        links = side_chat_service.list_session_links()

        assert len(links) == 1
        assert links[0]["parent_session_id"] == "parent-1"
        assert links[0]["fork_turn_num"] == 3


def _async_value(value):
    async def _call(*_args, **_kwargs):
        return value

    return _call


class TestHttp:
    @pytest.mark.asyncio
    async def test_post_passes_the_fork_turn_to_the_service(self, monkeypatch) -> None:
        seen: dict = {}

        async def fake_create(parent, request_id=None, fork_turn_num=None):
            seen.update(parent=parent, request_id=request_id, fork_turn_num=fork_turn_num)
            return {
                "session_id": "child",
                "parent_session_id": parent,
                "fork_turn_num": fork_turn_num,
                "inherited": True,
                "deduplicated": False,
            }

        monkeypatch.setattr(side_chat_http, "create_side_chat", fake_create)
        request = SimpleNamespace(
            json=lambda: {"parent_session_id": "parent-1", "fork_turn_num": 4}
        )

        response = await side_chat_http.create_side_chat_handler(request)

        assert response.status_code == 200
        assert seen == {"parent": "parent-1", "request_id": None, "fork_turn_num": 4}
        assert json.loads(response.description)["fork_turn_num"] == 4

    @pytest.mark.asyncio
    async def test_an_invalid_fork_turn_is_a_400(self, monkeypatch) -> None:
        async def fake_create(*_args, **_kwargs):
            raise ValueError("invalid fork_turn_num")

        monkeypatch.setattr(side_chat_http, "create_side_chat", fake_create)
        request = SimpleNamespace(
            json=lambda: {"parent_session_id": "parent-1", "fork_turn_num": 0}
        )

        response = await side_chat_http.create_side_chat_handler(request)

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_the_links_route_answers_the_branch_table(self, monkeypatch) -> None:
        monkeypatch.setattr(
            side_chat_http,
            "list_session_links",
            lambda: [
                {
                    "child_session_id": "child",
                    "parent_session_id": "parent",
                    "fork_turn_num": 2,
                }
            ],
        )

        response = await side_chat_http.list_side_chat_links_handler(None)

        payload = json.loads(response.description)
        assert payload["links"][0]["fork_turn_num"] == 2
