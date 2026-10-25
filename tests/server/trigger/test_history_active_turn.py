"""The history endpoint's ``include_active_turn`` response shape.

One round trip for a reconnecting client: the persisted page plus the running
turn's text snapshot. The default shape stays the plain row list so every
existing caller keeps working.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [pytest.mark.unit]


class _Request:
    def __init__(self, **params: str) -> None:
        self.query_params = params


def _run_handler(request: _Request):
    from server.trigger.http import messages as http_messages

    # Robyn's @app.get wraps the handler; call the unwrapped coroutine so the
    # test sees the value the framework serializes.
    handler = http_messages.get_history_by_turn_page.__wrapped__  # type: ignore[attr-defined]
    return asyncio.run(handler(request))


def _base_params(**extra: str) -> dict[str, str]:
    params = {
        "session_id": "sess-h",
        "min_turn_num": "1",
        "turn_page_size": "10",
        "turn_page_num": "1",
    }
    params.update(extra)
    return params


def test_history_without_the_flag_stays_a_plain_row_list(monkeypatch: pytest.MonkeyPatch):
    from server.trigger.http import messages as http_messages

    rows = [{"id": 1, "turn_num": 1}]
    monkeypatch.setattr(http_messages, "_get_history_by_turn_page", lambda *args: rows)

    assert _run_handler(_Request(**_base_params())) == rows


def test_history_with_the_flag_returns_messages_and_active_turn(monkeypatch: pytest.MonkeyPatch):
    from context_engine.store import core as mem_core
    from server.service import stream_dispatch
    from server.trigger.http import messages as http_messages

    rows = [{"id": 1, "turn_num": 1}]
    snapshot = {
        "text": "partial answer",
        "has_reasoning": True,
        "seq": 9,
        "turn_id": "turn-h",
    }
    monkeypatch.setattr(http_messages, "_get_history_by_turn_page", lambda *args: rows)
    monkeypatch.setattr(stream_dispatch, "active_turn_snapshot", lambda sid: dict(snapshot))
    monkeypatch.setattr(mem_core, "get_max_turn_num", lambda sid: 4)

    result = _run_handler(_Request(**_base_params(include_active_turn="1")))

    assert result["messages"] == rows
    assert result["active_turn"] == {**snapshot, "turn_num": 5}


def test_history_with_the_flag_and_no_running_turn(monkeypatch: pytest.MonkeyPatch):
    from server.service import stream_dispatch
    from server.trigger.http import messages as http_messages

    monkeypatch.setattr(http_messages, "_get_history_by_turn_page", lambda *args: [])
    monkeypatch.setattr(stream_dispatch, "active_turn_snapshot", lambda sid: None)

    result = _run_handler(_Request(**_base_params(include_active_turn="true")))

    assert result == {"messages": [], "active_turn": None}
