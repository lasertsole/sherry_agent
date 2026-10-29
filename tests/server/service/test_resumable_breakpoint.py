"""The resumable-breakpoint register (unit).

Contract: one breakpoint per session, dual-written (mem for the hot path, DB for
a restart), readable from either tier, and validated against the LIVE transcript
by the marker id + the turn count the marker id was derived from. A value that
does not describe the trailing message must read as invalid — a continuation
would otherwise append to a message that is no longer the tail.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from runtime import state_register_mem
from server.service import resumable_breakpoint as rb

pytestmark = pytest.mark.unit

SESSION = "s-breakpoint"


class _FakeRegisterDB:
    """In-memory stand-in for the durable tier (never touches the real SQLite)."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], Any] = {}

    def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        return self.store.get((session_id, key), default)

    def set_state(self, session_id: str, key: str, value: Any) -> bool:
        self.store[(session_id, key)] = value
        return True

    def delete_state(self, session_id: str, key: str) -> bool:
        self.store.pop((session_id, key), None)
        return True


@pytest.fixture()
def durable(monkeypatch: pytest.MonkeyPatch):
    """Isolate both register tiers for every test."""
    fake = _FakeRegisterDB()
    monkeypatch.setattr(rb, "state_register_db", fake)
    state_register_mem.clear_session(SESSION)
    yield fake
    state_register_mem.clear_session(SESSION)


def _breakpoint(**overrides: Any) -> dict[str, Any]:
    payload = rb.build_breakpoint(
        marker_id="interrupted-thread-1",
        partial_text="half an answer",
        reason="cancelled",
        turn_id="turn-1",
        turn_seq=1,
        is_reasoning_only=False,
    )
    payload.update(overrides)
    return payload


def test_set_read_has_clear_round_trip(durable: _FakeRegisterDB) -> None:
    assert rb.read_breakpoint(SESSION) is None
    assert rb.has_breakpoint(SESSION) is False

    payload = _breakpoint()
    rb.set_breakpoint(SESSION, payload)

    assert rb.has_breakpoint(SESSION) is True
    assert rb.read_breakpoint(SESSION) == payload

    rb.clear_breakpoint(SESSION)
    assert rb.read_breakpoint(SESSION) is None
    assert (SESSION, rb.StateKey.RESUMABLE_BREAKPOINT) not in durable.store


def test_a_restart_still_reads_the_breakpoint_from_the_durable_tier(
    durable: _FakeRegisterDB,
) -> None:
    payload = _breakpoint()
    rb.set_breakpoint(SESSION, payload)
    # A restart loses the mem tier; the DB copy is what survives.
    state_register_mem.clear_session(SESSION)

    assert rb.read_breakpoint(SESSION) == payload
    # ... and the read promotes back into the hot tier.
    assert state_register_mem.get_state(SESSION, rb.StateKey.RESUMABLE_BREAKPOINT) == payload


def test_a_malformed_value_reads_as_nothing(durable: _FakeRegisterDB) -> None:
    state_register_mem.set_state(SESSION, rb.StateKey.RESUMABLE_BREAKPOINT, "garbage")

    assert rb.read_breakpoint(SESSION) is None
    assert rb.has_breakpoint(SESSION) is False


def test_breakpoint_is_valid_only_while_the_marker_is_the_trailing_message() -> None:
    breakpoint = _breakpoint()
    marker = AIMessage(content="[interrupted] half an answer", id="interrupted-thread-1")

    assert rb.breakpoint_is_valid([HumanMessage("q"), marker], breakpoint) is True
    # A later message moved the tail on.
    assert (
        rb.breakpoint_is_valid([HumanMessage("q"), marker, HumanMessage("next")], breakpoint)
        is False
    )
    # The marker is there but the turn count changed (a new human message landed).
    assert (
        rb.breakpoint_is_valid(
            [HumanMessage("q"), HumanMessage("next"), marker], _breakpoint(turn_seq=1)
        )
        is False
    )


def test_breakpoint_is_valid_rejects_malformed_payloads() -> None:
    marker = AIMessage(content="[interrupted] x", id="interrupted-thread-1")

    assert rb.breakpoint_is_valid([marker], None) is False
    assert rb.breakpoint_is_valid([marker], {}) is False
    assert rb.breakpoint_is_valid([marker], _breakpoint(marker_id="")) is False
    assert rb.breakpoint_is_valid([marker], _breakpoint(turn_seq="1")) is False
    assert rb.breakpoint_is_valid([], _breakpoint()) is False


def test_build_breakpoint_carries_the_full_identity() -> None:
    payload = _breakpoint()

    assert payload == {
        "marker_id": "interrupted-thread-1",
        "partial_text": "half an answer",
        "reason": "cancelled",
        "at_turn_id": "turn-1",
        "turn_seq": 1,
        "is_reasoning_only": False,
    }
