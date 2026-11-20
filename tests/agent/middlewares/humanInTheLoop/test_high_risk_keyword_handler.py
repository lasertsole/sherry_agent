"""HighRiskKeywordApprovalHandler: main-agent keyword card + executed guard."""

from __future__ import annotations

from typing import Any

import pytest

from agent.middlewares.humanInTheLoop import strategies
from agent.middlewares.humanInTheLoop.strategies import (
    ApprovalContext,
    ApprovalOutcome,
    HighRiskKeywordApprovalHandler,
)
from agent.middlewares.humanInTheLoop.types import HITLConfig
from agent.tools.pub_base.high_risk_guard_store import HighRiskGuardStore
from runtime import StateKey, state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "hitl-keyword-session"


class _FakeMiddleware:
    """The three collaborator methods the handler uses."""

    def __init__(self, operator: str | None = "operator") -> None:
        self.config = HITLConfig()
        self.approval = None
        self._operator = operator

    def _get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        return state_register_mem.get_state(session_id, key, default)

    def _set_state(self, session_id: str, key: str, value: Any) -> bool:
        return state_register_mem.set_state(session_id, key, value)

    def _turn_operator(self, state: dict) -> str | None:
        return self._operator


def _ctx(operator: str | None = "operator") -> tuple[ApprovalContext, ApprovalOutcome]:
    outcome = ApprovalOutcome()
    ctx = ApprovalContext(
        mw=_FakeMiddleware(operator),  # type: ignore[arg-type]
        state={},
        runtime=None,  # type: ignore[arg-type]
        session_id=SESSION,
        outcome=outcome,
    )
    return ctx, outcome


def _call(name: str = "terminal", args: dict | None = None) -> dict:
    return {"name": name, "args": args or {"commands": ["rm -rf build"]}, "id": "c1"}


@pytest.fixture()
def guard_db(tmp_path, monkeypatch) -> HighRiskGuardStore:
    path = tmp_path / "guard.db"
    monkeypatch.setenv("SHERRY_HIGH_RISK_GUARD_PATH", str(path))
    state_register_mem.delete_state(SESSION, StateKey.KEYWORD_APPROVALS)
    state_register_mem.delete_state(SESSION, StateKey.HITL_SESSION_YOLO)
    state_register_mem.delete_state(SESSION, StateKey.HEARTBEAT_SKIP)
    return HighRiskGuardStore(path=str(path))


def test_matches_only_the_scanned_tools():
    handler = HighRiskKeywordApprovalHandler()
    ctx, _ = _ctx()
    assert handler.matches(_call("terminal"), ctx) is True
    assert handler.matches(_call("python_repl"), ctx) is True
    assert handler.matches(_call("execute_code"), ctx) is True
    assert handler.matches(_call("mcp__stripe__charge"), ctx) is True
    assert handler.matches(_call("read_file", {"path": "a.py"}), ctx) is False
    assert handler.matches(_call("write_file", {"path": "a.py"}), ctx) is False


def test_no_keyword_hit_falls_through(guard_db):
    handler = HighRiskKeywordApprovalHandler()
    ctx, outcome = _ctx()
    assert handler.handle(_call("terminal", {"commands": ["pytest -q"]}), ctx) is False
    assert outcome.revised_tool_calls == [] and outcome.artificial_tool_messages == []


def test_an_executed_keyword_is_refused_with_its_first_result(guard_db):
    guard_db.mark_executed(SESSION, "rm -rf", "removed build")
    handler = HighRiskKeywordApprovalHandler()
    ctx, outcome = _ctx()
    assert handler.handle(_call(), ctx) is True
    assert outcome.revised_tool_calls == []
    assert "already ran" in outcome.artificial_tool_messages[0].content
    assert "removed build" in outcome.artificial_tool_messages[0].content


def test_a_recorded_approval_short_circuits_the_card(guard_db):
    state_register_mem.set_state(
        SESSION,
        StateKey.KEYWORD_APPROVALS,
        [{"keyword": "rm -rf", "tool": "terminal"}],
    )
    handler = HighRiskKeywordApprovalHandler()
    ctx, outcome = _ctx()
    assert handler.handle(_call(), ctx) is True
    assert len(outcome.revised_tool_calls) == 1
    assert outcome.artificial_tool_messages == []


def test_approve_records_the_approval_and_freezes_the_heartbeat(guard_db, monkeypatch):
    captured: list[dict] = []

    def fake_interrupt(request):
        # While parked, the heartbeat must already be frozen.
        assert state_register_mem.get_state(SESSION, StateKey.HEARTBEAT_SKIP, False) is True
        captured.append(request)
        return {"decisions": [{"type": "approve"}]}

    monkeypatch.setattr(strategies, "interrupt", fake_interrupt)
    handler = HighRiskKeywordApprovalHandler()
    ctx, outcome = _ctx()
    assert handler.handle(_call(), ctx) is True
    assert len(outcome.revised_tool_calls) == 1
    approvals = state_register_mem.get_state(SESSION, StateKey.KEYWORD_APPROVALS, [])
    assert approvals and approvals[0]["keyword"] == "rm -rf"
    assert approvals[0]["tool"] == "terminal"
    assert state_register_mem.get_state(SESSION, StateKey.HEARTBEAT_SKIP, False) is False
    assert captured, "the card must carry one action request"
    request = captured[0]
    actions = request["action_requests"] if isinstance(request, dict) else request.action_requests
    first = actions[0]
    assert (first["name"] if isinstance(first, dict) else first.name) == "terminal"


def test_reject_denies_and_records_no_approval(guard_db, monkeypatch):
    monkeypatch.setattr(
        strategies, "interrupt", lambda request: {"decisions": [{"type": "reject"}]}
    )
    handler = HighRiskKeywordApprovalHandler()
    ctx, outcome = _ctx()
    assert handler.handle(_call(), ctx) is True
    assert outcome.revised_tool_calls == []
    assert "rm -rf" in outcome.artificial_tool_messages[0].content
    assert state_register_mem.get_state(SESSION, StateKey.KEYWORD_APPROVALS, []) == []


def test_a_headless_turn_auto_denies_without_interrupting(guard_db, monkeypatch):
    def boom(request):
        raise AssertionError("a headless turn must not interrupt")

    monkeypatch.setattr(strategies, "interrupt", boom)
    handler = HighRiskKeywordApprovalHandler()
    ctx, outcome = _ctx(operator=None)
    assert handler.handle(_call(), ctx) is True
    assert outcome.revised_tool_calls == []
    assert "auto-denied" in outcome.artificial_tool_messages[0].content


def test_yolo_skips_a_reversible_tool_but_not_an_irreversible_one(guard_db, monkeypatch):
    state_register_mem.set_state(SESSION, StateKey.HITL_SESSION_YOLO, True)
    handler = HighRiskKeywordApprovalHandler()
    ctx, outcome = _ctx()
    assert handler.handle(_call("terminal"), ctx) is False
    assert outcome.revised_tool_calls == []

    calls: list[dict] = []

    def fake_interrupt(request):
        calls.append(request)
        return {"decisions": [{"type": "approve"}]}

    monkeypatch.setattr(strategies, "interrupt", fake_interrupt)
    ctx2, outcome2 = _ctx()
    assert (
        handler.handle(_call("mcp__stripe__charge", {"card_number": "4242 4242 4242 4242"}), ctx2)
        is True
    )
    assert len(outcome2.revised_tool_calls) == 1
    assert calls, "an irreversible tool must still ask under full_access"
