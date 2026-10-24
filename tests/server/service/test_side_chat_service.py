"""Ephemeral side-chat links, inherited context, and purge hooks."""

from __future__ import annotations

import pytest

from server.DAO import side_chat_store
from server.service import side_chat_service


@pytest.fixture(autouse=True)
def isolated_side_chat_store(tmp_path, monkeypatch):
    monkeypatch.setattr(side_chat_store, "_DB_PATH", tmp_path / "side_chats.db")


@pytest.mark.unit
def test_store_create_get_list_and_delete() -> None:
    side_chat_store.create_side_chat("child-1", "parent-1", "context")
    side_chat_store.create_side_chat("child-2", "parent-1")
    side_chat_store.create_side_chat("child-3", "parent-2")

    row = side_chat_store.get_side_chat("child-1")
    assert row is not None
    assert row["parent_session_id"] == "parent-1"
    assert row["context_text"] == "context"
    assert row["fork_mode"] == 0
    assert side_chat_store.list_children("parent-1") == ["child-1", "child-2"]
    assert side_chat_store.side_chat_ids() == {"child-1", "child-2", "child-3"}
    assert [entry["fork_mode"] for entry in side_chat_store.all_side_chats()] == [0, 0, 0]

    side_chat_store.create_side_chat("child-4", "parent-2", fork_mode=True)
    forked = side_chat_store.get_side_chat("child-4")
    assert forked is not None and forked["fork_mode"] == 1

    assert side_chat_store.delete_side_chat("child-2") is True
    assert side_chat_store.get_side_chat("child-2") is None
    assert side_chat_store.delete_side_chat("child-2") is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_side_chat_stores_parent_link_and_context(monkeypatch) -> None:
    async def fake_context(_parent: str) -> str:
        return "<side_chat_context>history</side_chat_context>"

    monkeypatch.setattr(side_chat_service, "build_parent_context", fake_context)

    result = await side_chat_service.create_side_chat("parent-1")

    assert result["parent_session_id"] == "parent-1"
    assert result["inherited"] is True
    assert len(result["session_id"]) == 32
    row = side_chat_store.get_side_chat(result["session_id"])
    assert row is not None
    assert row["context_text"] == "<side_chat_context>history</side_chat_context>"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_checkpoint_fork_copies_parent_model_messages(monkeypatch) -> None:
    side_chat_store.create_side_chat("child-1", "parent-1", "fallback text", fork_mode=True)
    import context_engine.store.core as store_core
    import agent.state_port as state_port
    from langchain_core.messages import AIMessage, HumanMessage

    monkeypatch.setattr(store_core, "session_has_messages", lambda _sid: False)

    async def fake_read_messages(_parent: str):
        return [
            HumanMessage(content="parent question"),
            AIMessage(content="parent answer"),
        ]

    monkeypatch.setattr(state_port, "read_messages", fake_read_messages)

    inherited = await side_chat_service.get_checkpoint_fork_for_turn("child-1")

    assert inherited is not None
    assert len(inherited) == 3
    assert [getattr(message, "content", "") for message in inherited[:2]] == [
        "parent question",
        "parent answer",
    ]
    assert all(message.additional_kwargs.get("side_chat_context") is True for message in inherited)
    assert "complete fork" in str(inherited[-1].content)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_checkpoint_fork_stops_at_the_last_completed_answer(monkeypatch) -> None:
    side_chat_store.create_side_chat("child-1", "parent-1", fork_mode=True)
    import context_engine.store.core as store_core
    import agent.state_port as state_port
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    monkeypatch.setattr(store_core, "session_has_messages", lambda _sid: False)

    async def fake_read_messages(_parent: str):
        return [
            HumanMessage(content="parent question"),
            AIMessage(content="parent answer"),
            HumanMessage(content="unfinished question"),
            AIMessage(
                content="",
                tool_calls=[{"name": "terminal", "args": {}, "id": "call-1"}],
            ),
            ToolMessage(content="unfinished result", tool_call_id="call-1"),
        ]

    monkeypatch.setattr(state_port, "read_messages", fake_read_messages)

    inherited = await side_chat_service.get_checkpoint_fork_for_turn("child-1")

    assert inherited is not None
    # The dangling tool call (a pending HITL approval / interrupted run) and the
    # user turn it belonged to are not replayed into the child.
    assert [message.content for message in inherited[:-1]] == [
        "parent question",
        "parent answer",
    ]
    assert all(message.additional_kwargs.get("side_chat_context") is True for message in inherited)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_checkpoint_fork_falls_back_when_no_answer_completed(monkeypatch) -> None:
    side_chat_store.create_side_chat(
        "child-1", "parent-1", "<side_chat_context>fallback</side_chat_context>", fork_mode=True
    )
    import context_engine.store.core as store_core
    import agent.state_port as state_port
    from langchain_core.messages import AIMessage, HumanMessage

    monkeypatch.setattr(store_core, "session_has_messages", lambda _sid: False)

    async def fake_read_messages(_parent: str):
        return [
            HumanMessage(content="waiting for the first answer"),
            AIMessage(content="", tool_calls=[{"name": "terminal", "args": {}, "id": "c"}]),
        ]

    monkeypatch.setattr(state_port, "read_messages", fake_read_messages)

    inherited = await side_chat_service.get_checkpoint_fork_for_turn("child-1")

    assert inherited is not None
    assert len(inherited) == 1
    assert "fallback" in str(inherited[0].content)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_side_chat_copies_parent_session_settings(monkeypatch) -> None:
    async def fake_context(_parent: str) -> str:
        return ""

    monkeypatch.setattr(side_chat_service, "build_parent_context", fake_context)

    import server.service.session_settings_service as settings

    parent_values = {
        "llm_main_model": {"provider": "deepseek", "model": "deepseek-flash"},
        "project_dir": "/home/honor/WorkBuddy",
        "project_dir_announced": "/home/honor/WorkBuddy",
        "git_head_announced": "checkpoint@d2b67de",
        "agent_config": {"tools": ["read_file"]},
    }
    written: dict[str, object] = {}

    def fake_read(session_id: str, key: str):
        assert session_id == "parent-1"
        return parent_values.get(key)

    def fake_write(session_id: str, key: str, value: object) -> None:
        written[key] = value

    monkeypatch.setattr(settings, "_read_value", fake_read)
    monkeypatch.setattr(settings, "_write_value", fake_write)

    result = await side_chat_service.create_side_chat("parent-1")

    assert written == parent_values
    row = side_chat_store.get_side_chat(result["session_id"])
    assert row is not None and row["fork_mode"] == 1


@pytest.mark.unit
def test_fork_context_is_not_used_after_the_child_has_messages(monkeypatch) -> None:
    side_chat_store.create_side_chat("child-1", "parent-1", fork_mode=True)
    import context_engine.store.core as store_core

    monkeypatch.setattr(store_core, "session_has_messages", lambda _sid: True)
    assert side_chat_service.get_context_for_turn("child-1") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_side_chat_refuses_a_side_chat_parent() -> None:
    side_chat_store.create_side_chat("child-1", "parent-1")

    with pytest.raises(ValueError, match="cannot parent"):
        await side_chat_service.create_side_chat("child-1")


@pytest.mark.unit
def test_context_is_injected_only_until_the_child_has_messages(monkeypatch) -> None:
    side_chat_store.create_side_chat("child-1", "parent-1", "<context/>")

    import context_engine.store.core as store_core

    monkeypatch.setattr(store_core, "session_has_messages", lambda _sid: False)
    assert side_chat_service.get_context_for_turn("child-1") == "<context/>"

    monkeypatch.setattr(store_core, "session_has_messages", lambda _sid: True)
    assert side_chat_service.get_context_for_turn("child-1") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_side_chat_cancels_and_purges(monkeypatch) -> None:
    side_chat_store.create_side_chat("child-1", "parent-1")
    calls: list[tuple[str, str]] = []

    async def fake_cancel(session_id: str) -> None:
        calls.append(("cancel", session_id))

    async def fake_close(_session_id: str) -> None:
        calls.append(("browser", _session_id))

    async def fake_subtree(session_id: str) -> None:
        calls.append(("subtree", session_id))

    async def fake_purge(session_id: str) -> None:
        calls.append(("purge", session_id))

    class _Browser:
        close_session = staticmethod(fake_close)

    import server.service.browser_manager as browser_manager
    import server.service.session_cleanup_service as cleanup
    import server.service.turn_runner as turn_runner

    monkeypatch.setattr(turn_runner, "cancel_session_turn", fake_cancel)
    monkeypatch.setattr(browser_manager, "get_browser_manager", lambda: _Browser())
    monkeypatch.setattr(cleanup, "purge_subagent_subtree_for_session", fake_subtree)
    monkeypatch.setattr(cleanup, "purge_session_data", fake_purge)

    assert await side_chat_service.delete_side_chat("child-1") is True
    assert calls == [
        ("cancel", "child-1"),
        ("browser", "child-1"),
        ("subtree", "child-1"),
        ("purge", "child-1"),
    ]
    assert side_chat_store.get_side_chat("child-1") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_purge_session_cascades_side_chat_children(monkeypatch) -> None:
    calls: list[str] = []

    import context_engine.session_continuity as continuity
    import server.service.session_cleanup_service as cleanup
    import server.service.side_chat_service as side_chats

    async def fake_save(_session_id: str) -> None:
        pass

    async def fake_subtree(_session_id: str) -> None:
        calls.append("subtree")

    async def fake_children(parent_id: str) -> int:
        calls.append(f"side:{parent_id}")
        return 1

    async def fake_purge(session_id: str) -> None:
        calls.append(f"parent-data:{session_id}")

    monkeypatch.setattr(continuity, "auto_save_on_session_end", fake_save)
    monkeypatch.setattr(cleanup, "purge_subagent_subtree_for_session", fake_subtree)
    monkeypatch.setattr(side_chats, "purge_side_chat_children", fake_children)
    monkeypatch.setattr(cleanup, "purge_session_data", fake_purge)

    await cleanup.purge_session("parent-1")

    assert calls == ["subtree", "side:parent-1", "parent-data:parent-1"]


@pytest.mark.unit
def test_session_list_hides_side_chats(monkeypatch) -> None:
    side_chat_store.create_side_chat("child-1", "parent-1")
    import server.service.messages as messages

    monkeypatch.setattr(
        messages,
        "get_session_ids",
        lambda: [
            {"session_id": "parent-1", "last_time": "1", "title": "Parent"},
            {"session_id": "child-1", "last_time": "2", "title": "Side"},
        ],
    )

    assert messages.get_session_list() == [
        {"session_id": "parent-1", "last_time": "1", "title": "Parent"}
    ]
