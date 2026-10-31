"""The resumable-breakpoint decision and its turn-boundary rules (unit).

The breakpoint is recorded ONLY for a pure-text interruption: a fresh marker
write, no healed tool calls, and something to continue from. Every new turn
clears it; a continuation turn consumes it itself and refuses to run when the
transcript moved on.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from agent.state_port import InterruptMarkerWrite
from server.service import messages as messages_module
from server.service.messages import _ContinueTurn, _GenerateTurn
from server.service.resumable_breakpoint import BreakpointStaleError

pytestmark = pytest.mark.unit

SESSION = "s-continue"


@pytest.fixture(autouse=True)
def _fresh_breakpoint(monkeypatch: pytest.MonkeyPatch):
    """Both register tiers stay in-process (never the real SQLite)."""
    store: dict = {}
    monkeypatch.setattr(messages_module, "read_breakpoint", lambda _sid: store.get("value"))
    monkeypatch.setattr(
        messages_module, "set_breakpoint", lambda _sid, payload: store.__setitem__("value", payload)
    )
    monkeypatch.setattr(messages_module, "clear_breakpoint", lambda _sid: store.pop("value", None))
    yield store
    store.clear()


def _write(**overrides) -> InterruptMarkerWrite:
    values = {
        "marker_id": "interrupted-t-1",
        "wrote_checkpointer": True,
        "healed_calls": 0,
        "turn_seq": 1,
    }
    values.update(overrides)
    return InterruptMarkerWrite(**values)


class TestRecordBreakpoint:
    def test_records_a_pure_text_interruption(self, _fresh_breakpoint) -> None:
        messages_module._record_resumable_breakpoint(
            SESSION,
            _write(),
            reason="cancelled",
            turn_id="turn-1",
            partial_text="half an answer",
            has_visible_text=True,
            has_reasoning=False,
        )

        payload = _fresh_breakpoint["value"]
        assert payload["marker_id"] == "interrupted-t-1"
        assert payload["partial_text"] == "half an answer"
        assert payload["turn_seq"] == 1
        assert payload["is_reasoning_only"] is False

    def test_a_tool_call_interruption_is_not_resumable(self, _fresh_breakpoint) -> None:
        messages_module._record_resumable_breakpoint(
            SESSION,
            _write(healed_calls=2),
            reason="cancelled",
            turn_id="turn-1",
            partial_text="text",
            has_visible_text=True,
            has_reasoning=False,
        )

        assert "value" not in _fresh_breakpoint

    def test_an_idempotent_marker_rewrite_is_not_resumable(self, _fresh_breakpoint) -> None:
        messages_module._record_resumable_breakpoint(
            SESSION,
            _write(wrote_checkpointer=False),
            reason="cancelled",
            turn_id="turn-1",
            partial_text="text",
            has_visible_text=True,
            has_reasoning=False,
        )

        assert "value" not in _fresh_breakpoint

    def test_a_failed_marker_write_is_not_resumable(self, _fresh_breakpoint) -> None:
        messages_module._record_resumable_breakpoint(
            SESSION,
            None,
            reason="heartbeat_timeout",
            turn_id="turn-1",
            partial_text="text",
            has_visible_text=True,
            has_reasoning=False,
        )

        assert "value" not in _fresh_breakpoint

    def test_reasoning_only_gets_its_own_prompt_flag(self, _fresh_breakpoint) -> None:
        messages_module._record_resumable_breakpoint(
            SESSION,
            _write(),
            reason="heartbeat_timeout",
            turn_id="turn-2",
            partial_text="",
            has_visible_text=False,
            has_reasoning=True,
        )

        assert _fresh_breakpoint["value"]["is_reasoning_only"] is True
        assert _fresh_breakpoint["value"]["reason"] == "heartbeat_timeout"

    def test_nothing_to_continue_from_is_skipped(self, _fresh_breakpoint) -> None:
        messages_module._record_resumable_breakpoint(
            SESSION,
            _write(),
            reason="cancelled",
            turn_id="turn-1",
            partial_text="   ",
            has_visible_text=False,
            has_reasoning=False,
        )

        assert "value" not in _fresh_breakpoint


class TestTurnBoundaries:
    def test_a_new_generation_turn_clears_the_breakpoint(self, _fresh_breakpoint) -> None:
        _fresh_breakpoint["value"] = {"marker_id": "x"}

        _GenerateTurn(SESSION, messages=[HumanMessage("hi")])

        assert "value" not in _fresh_breakpoint

    def test_a_continue_turn_keeps_the_breakpoint_for_itself(self, _fresh_breakpoint) -> None:
        payload = {"marker_id": "interrupted-t-1", "turn_seq": 1, "partial_text": "half"}
        _fresh_breakpoint["value"] = payload

        turn = _ContinueTurn(SESSION)

        assert turn._clears_resumable_breakpoint is False
        assert _fresh_breakpoint["value"] == payload

    @pytest.mark.asyncio
    async def test_a_continue_turn_without_a_breakpoint_refuses(self) -> None:
        turn = _ContinueTurn(SESSION)
        await turn._prepare()

        with pytest.raises(BreakpointStaleError):
            await turn._create_source()
