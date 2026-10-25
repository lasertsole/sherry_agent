"""User-requested compaction: the compact_now queue seam (ZCode UserRequested)."""

from __future__ import annotations

import json

import pytest
from langchain.agents.middleware import ModelRequest

from agent.middlewares.summarization.overflow import ROUTE_COMPACT_ONLY
from agent.tools.compact_now import build_compact_now_tool
from runtime import state_register_mem
from runtime.session.state_keys import StateKey

pytestmark = [pytest.mark.unit]


def test_compact_now_queues_the_request_and_forces_recovery():
    tool = build_compact_now_tool()
    payload = json.loads(tool._run(session_id="sess-compact", reason="operator asked"))

    assert payload["ok"] is True
    assert payload["status"] == "queued"
    assert state_register_mem.get_state(
        "sess-compact", StateKey.SUMMARIZATION_COMPACT_REQUESTED, False
    )
    assert state_register_mem.get_state(
        "sess-compact", StateKey.SUMMARIZATION_FORCE_RECOVERY, False
    )


def test_compact_now_without_a_session_is_refused():
    payload = json.loads(build_compact_now_tool()._run(session_id="  "))
    assert payload["ok"] is False
    assert "session" in payload["error"]


def test_compact_now_tool_metadata():
    tool = build_compact_now_tool()
    assert tool.name == "compact_now"
    assert tool.metadata["side_effect_scope"] == "session"
    assert tool.metadata["idempotent"] is False


def test_t2_dispatch_consumes_the_request_and_compacts(monkeypatch: pytest.MonkeyPatch):
    from agent.middlewares.summarization.core import Summarization

    calls: list[tuple[str, str]] = []

    class _Harness(Summarization):
        def _execute_compact(self, request, route, session_id, trigger):  # type: ignore[override]
            calls.append((route, trigger))
            return request

    state_register_mem.set_state("sess-dispatch", StateKey.SUMMARIZATION_COMPACT_REQUESTED, True)
    harness = _Harness(model=None, main_llm_context_window=131_072)
    request = ModelRequest(model=None, messages=[], state={"session_id": "sess-dispatch"})

    out = harness._before_call_dispatch(request, "sess-dispatch")

    assert out is request
    assert calls == [(ROUTE_COMPACT_ONLY, "USER")]
    assert not state_register_mem.get_state(
        "sess-dispatch", StateKey.SUMMARIZATION_COMPACT_REQUESTED, False
    )
