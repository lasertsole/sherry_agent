"""The rewind service and its fences: turn gate, filter wiring, stale injections.

Everything here runs against real stores (tmp SQLite) with the session-state
register as the only shared component: the point is the integration — a rewind
must change what /get_history_by_turn_page returns, must refuse while a turn is
running, and must invalidate the two asynchronous injections that carry
pre-rewind work (a subagent announcement, a memory nudge).
"""

import asyncio

import pytest

from runtime.session.conversation_branch import apply_rewind, clear_branch

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]

SESSION = "s-rewind-service"


@pytest.fixture(autouse=True)
def _clean_branch():
    """The branch record lives in the process-wide state register: isolate it."""
    clear_branch(SESSION)
    yield
    clear_branch(SESSION)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A tmp mes_memory database with three persisted messages (one per turn)."""
    import context_engine.store.core as core
    from context_engine.store import db as store_db

    db_path = tmp_path / "mes_memory.db"
    monkeypatch.setattr(store_db, "_db_path", db_path)
    monkeypatch.setattr(store_db, "_db", None)
    monkeypatch.setattr(core, "_db", store_db.get_db())

    from langchain_core.messages import HumanMessage

    for index in (1, 2, 3):
        asyncio.run(core.add_messages(SESSION, [HumanMessage(content=f"message-{index}")]))
    yield


def _page():
    from context_engine.store.core import get_history_by_turn_page

    return get_history_by_turn_page(SESSION, min_turn_num=1, turn_page_size=10, turn_page_num=1)


def test_a_rewind_hides_the_cut_range_from_readers(store):
    ids_before = [row["id"] for row in _page()]
    assert len(ids_before) == 3

    cut = min(ids_before)  # keep only the oldest message
    apply_rewind(SESSION, cut_after_message_id=cut, tip_message_id=max(ids_before))

    ids_after = [row["id"] for row in _page()]
    assert ids_after == [cut]


def test_messages_written_after_a_rewind_are_visible(store):
    from context_engine.store import core as store_core
    from langchain_core.messages import HumanMessage

    ids = [row["id"] for row in _page()]
    cut = min(ids)
    apply_rewind(SESSION, cut_after_message_id=cut, tip_message_id=max(ids))

    asyncio.run(store_core.add_messages(SESSION, [HumanMessage(content="after the rewind")]))

    visible = [row["id"] for row in _page()]
    assert cut in visible
    assert max(visible) > max(ids)  # the new message is on the active branch


def test_the_service_refuses_while_a_turn_is_running(store, monkeypatch):
    from server.service import rewind_service

    async def busy(_session_id: str) -> bool:
        return True

    monkeypatch.setattr(
        "server.service.session_settings_service.session_turn_active", busy, raising=True
    )

    with pytest.raises(ValueError, match="turn is running"):
        asyncio.run(rewind_service.apply_session_rewind(SESSION, 1))


def test_the_service_refuses_a_cut_that_is_not_a_message(store):
    from server.service import rewind_service

    with pytest.raises(ValueError, match="not a message"):
        asyncio.run(rewind_service.apply_session_rewind(SESSION, 999))


def test_the_service_refuses_when_the_cut_is_the_tip(store):
    from server.service import rewind_service

    ids = [row["id"] for row in _page()]
    with pytest.raises(ValueError, match="already at the tip"):
        asyncio.run(rewind_service.apply_session_rewind(SESSION, max(ids)))


def test_the_service_applies_and_clears_the_pending_approval(store, monkeypatch):
    from server.service import rewind_service

    async def idle(_session_id: str) -> bool:
        return False

    monkeypatch.setattr(
        "server.service.session_settings_service.session_turn_active", idle, raising=True
    )
    cleared: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        "server.service.turn_runner.set_hitl_pending",
        lambda sid, value: cleared.append((sid, value)),
        raising=True,
    )

    ids = [row["id"] for row in _page()]
    payload = asyncio.run(rewind_service.apply_session_rewind(SESSION, min(ids)))

    assert payload["branch_generation"] == 1
    assert payload["tip_message_id"] == max(ids)
    # The abandoned branch's approval must not be resumable.
    assert cleared == [(SESSION, False)]


def test_the_state_view_reports_whether_a_rewind_is_allowed(store, monkeypatch):
    from server.service import rewind_service

    async def busy(_session_id: str) -> bool:
        return True

    monkeypatch.setattr(
        "server.service.session_settings_service.session_turn_active", busy, raising=True
    )
    state = asyncio.run(rewind_service.rewind_state(SESSION))

    assert state["can_rewind"] is False
    assert state["branch_generation"] == 0


def test_an_announce_from_before_a_rewind_is_dropped():
    """The announce fence: a run spawned before the cut never speaks again."""
    from agent.tools.subagent.announce.core import _was_rewound_away
    from agent.tools.subagent.types.registry import SubagentRunRecord
    from runtime import StateKey, state_register_mem
    from runtime.session.conversation_branch import branch_generation

    child = f"agent:main:subagent:{SESSION}-child"
    run = SubagentRunRecord(
        run_id="r1",
        child_session_key=child,
        requester_session_key=f"agent:main:session:{SESSION}",
        spawned_by=f"agent:main:session:{SESSION}",
        task="rewind fence probe",
    )

    # No stamp at all: fail open (never drop a legitimate announcement).
    assert _was_rewound_away(run) is False

    # Spawn stamps the parent's generation…
    state_register_mem.set_state(
        child, StateKey.SPAWNED_BRANCH_GENERATION, branch_generation(SESSION)
    )
    assert _was_rewound_away(run) is False

    # …a later rewind moves the parent on, and the run is now stale.
    apply_rewind(SESSION, cut_after_message_id=5, tip_message_id=9)
    assert _was_rewound_away(run) is True

    # A run stamped after the rewind is current again.
    state_register_mem.set_state(
        child, StateKey.SPAWNED_BRANCH_GENERATION, branch_generation(SESSION)
    )
    assert _was_rewound_away(run) is False
    state_register_mem.delete_state(child, StateKey.SPAWNED_BRANCH_GENERATION)


def test_a_pending_interrupt_from_before_a_rewind_is_suppressed(store):
    """The checkpoint still holds the abandoned branch's approval: hide it."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from server.service.messages import _interrupt_predates_rewind

    old = SimpleNamespace(created_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat())
    naive = SimpleNamespace(
        created_at=(datetime.now(UTC) - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    )

    assert _interrupt_predates_rewind(SESSION, old) is False  # no rewind yet

    apply_rewind(SESSION, cut_after_message_id=5, tip_message_id=9)

    assert _interrupt_predates_rewind(SESSION, old) is True  # raised before the cut
    # …and an approval raised after the cut still shows.
    fresh = SimpleNamespace(created_at=datetime.now(UTC).isoformat())
    assert _interrupt_predates_rewind(SESSION, fresh) is False
    assert _interrupt_predates_rewind(SESSION, naive) is True  # naive stamps read as UTC
    assert _interrupt_predates_rewind(SESSION, SimpleNamespace(created_at=None)) is False
