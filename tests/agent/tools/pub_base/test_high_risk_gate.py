"""Shared high-risk gate: the decision table used outside the middleware chain.

The gate is the ONE implementation shared by ``ToolGuardrails``, the PTC RPC
server and the LDW leaf's guarded tool face, so these tests pin the exact
policy: metadata short-circuit, ledger lifecycle, keyword scan, approvals,
YOLO/confirm_all and the two switches.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from agent.tools.pub_base import high_risk_gate as gate_mod
from agent.tools.pub_base.high_risk_gate import (
    check_high_risk_call,
    record_high_risk_intent,
    record_high_risk_result,
    replay_path_gate_enabled,
)
from agent.tools.pub_base.high_risk_guard_store import HighRiskGuardStore
from runtime import StateKey, state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

MCP = SimpleNamespace(
    name="mcp__stripe__charge",
    metadata={"high_risk": True, "idempotent": False},
)
TERMINAL = SimpleNamespace(
    name="terminal",
    metadata={"high_risk": False, "idempotent": False, "scan_keywords": True},
)
GRAY = SimpleNamespace(name="read_file", metadata={"idempotent": True})
GROUPED = SimpleNamespace(
    name="mcp__host__grouped",
    metadata={"high_risk": True, "idempotent_actions": ("status",)},
)

SESSION = "gate-session"


@pytest.fixture()
def store(tmp_path) -> HighRiskGuardStore:
    return HighRiskGuardStore(path=str(tmp_path / "gate.db"))


def _check(store, tool=MCP, args=None, session=SESSION, scope="", note=""):
    return check_high_risk_call(
        session_id=session,
        scope=scope or session,
        tool_name=tool.name,
        tool=tool,
        args=args or {},
        note=note,
        store=store,
    )


class _ExplodingStore:
    """Any attribute access means the metadata short-circuit failed."""

    def __getattr__(self, name: str):  # noqa: D105 - a spy, not a store
        raise AssertionError(f"the ledger must not be touched (store.{name})")


# ---------------------------------------------------------------------------
# Short-circuit + policy predicates
# ---------------------------------------------------------------------------


def test_an_ordinary_tool_never_touches_the_store() -> None:
    decision = _check(_ExplodingStore(), tool=GRAY, args={"file_path": "a.py"})
    assert decision.refusal is None
    assert decision.scope == ""
    assert decision.ledgered is False and decision.scanned is False


def test_a_grouped_tool_is_only_ledgered_for_non_idempotent_actions(store) -> None:
    clean = _check(store, tool=GROUPED, args={"action": "status"})
    assert clean.refusal is None

    store.record_op(SESSION, GROUPED.name, {"action": "charge"}, "charged")
    ledgered = _check(store, tool=GROUPED, args={"action": "charge"})
    assert ledgered.refusal is not None and "charged" in ledgered.refusal


# ---------------------------------------------------------------------------
# Ledger lifecycle
# ---------------------------------------------------------------------------


def test_committed_row_refuses_with_the_first_result(store) -> None:
    store.record_op(SESSION, MCP.name, {"amount": 100}, "charged")
    decision = _check(store, args={"amount": 100})
    assert decision.refusal is not None
    assert "already ran" in decision.refusal and "charged" in decision.refusal


def test_failed_row_allows_a_retry(store) -> None:
    store.record_intent(SESSION, MCP.name, {"amount": 1})
    store.mark_failed(SESSION, MCP.name, {"amount": 1})
    assert _check(store, args={"amount": 1}).refusal is None


def test_in_flight_row_from_this_process_refuses(store) -> None:
    store.record_intent(SESSION, MCP.name, {"amount": 1})
    decision = _check(store, args={"amount": 1})
    assert decision.refusal is not None
    assert "still in progress" in decision.refusal


def test_in_flight_row_from_a_previous_boot_reports_an_unknown_outcome(store, tmp_path) -> None:
    store.record_intent(SESSION, MCP.name, {"amount": 1})
    with sqlite3.connect(str(store.path)) as conn:
        conn.execute("UPDATE high_risk_ops SET boot_id = 'previous-boot'")
        conn.commit()
    decision = _check(store, args={"amount": 1})
    assert decision.refusal is not None
    assert "interrupted" in decision.refusal and "unknown" in decision.refusal


def test_recording_a_result_commits_or_fails_the_intent_row(store) -> None:
    record_high_risk_intent(
        scope=SESSION, tool_name=MCP.name, tool=MCP, args={"amount": 2}, store=store
    )
    assert store.check_op(SESSION, MCP.name, {"amount": 2})["status"] == "in_flight"

    record_high_risk_result(
        scope=SESSION,
        tool_name=MCP.name,
        tool=MCP,
        args={"amount": 2},
        result_text="charged",
        status="success",
        hit_keyword=None,
        store=store,
    )
    row = store.check_op(SESSION, MCP.name, {"amount": 2})
    assert row["status"] == "committed" and row["result_summary"] == "charged"

    record_high_risk_intent(
        scope=SESSION, tool_name=MCP.name, tool=MCP, args={"amount": 3}, store=store
    )
    record_high_risk_result(
        scope=SESSION,
        tool_name=MCP.name,
        tool=MCP,
        args={"amount": 3},
        result_text="boom",
        status="error",
        hit_keyword=None,
        store=store,
    )
    assert store.check_op(SESSION, MCP.name, {"amount": 3})["status"] == "failed"


def test_a_late_intent_never_clobbers_a_committed_row(store) -> None:
    store.record_op(SESSION, MCP.name, {"amount": 4}, "charged")
    store.record_intent(SESSION, MCP.name, {"amount": 4})
    row = store.check_op(SESSION, MCP.name, {"amount": 4})
    assert row["status"] == "committed"
    assert row["result_summary"] == "charged"


# ---------------------------------------------------------------------------
# Keyword scan + approvals + YOLO
# ---------------------------------------------------------------------------


def test_a_keyword_hit_returns_needs_approval(store) -> None:
    decision = _check(store, tool=TERMINAL, args={"commands": ["rm -rf /tmp/x"]})
    assert decision.refusal is not None
    assert "NEEDS_APPROVAL" in decision.refusal and "rm -rf" in decision.refusal
    assert decision.hit_keyword == "rm -rf"


def test_an_approved_keyword_passes(store) -> None:
    state_register_mem.set_state(
        SESSION, StateKey.KEYWORD_APPROVALS, [{"keyword": "rm -rf", "tool": "terminal"}]
    )
    decision = _check(store, tool=TERMINAL, args={"commands": ["rm -rf build"]})
    assert decision.refusal is None and decision.hit_keyword == "rm -rf"


def test_an_executed_keyword_refuses_a_repeat(store) -> None:
    store.mark_executed(SESSION, "password", "rotated")
    decision = _check(store, tool=TERMINAL, args={"commands": ["echo password"]})
    assert decision.refusal is not None
    assert "already ran" in decision.refusal and "rotated" in decision.refusal


def test_a_note_is_appended_to_the_refusal(store) -> None:
    decision = _check(
        store, tool=TERMINAL, args={"commands": ["echo password"]}, note="PTC has no card."
    )
    assert decision.refusal is not None and decision.refusal.endswith("PTC has no card.")


def test_a_full_access_child_skips_the_keyword_scan(store) -> None:
    child = "agent:main:subagent:gate-yolo"
    state_register_mem.set_state(child, StateKey.HITL_SESSION_YOLO, True)
    decision = _check(store, tool=TERMINAL, args={"commands": ["echo password"]}, session=child)
    assert decision.refusal is None


def test_confirm_all_refuses_a_child(store) -> None:
    child = "agent:main:subagent:gate-confirm"
    state_register_mem.set_state(child, StateKey.HITL_SESSION_CONFIRM_ALL, True)
    decision = _check(store, tool=TERMINAL, args={"commands": ["echo password"]}, session=child)
    assert decision.refusal is not None and "confirm_all" in decision.refusal


# ---------------------------------------------------------------------------
# Switches
# ---------------------------------------------------------------------------


def test_the_global_switch_disables_every_decision(store, monkeypatch) -> None:
    store.record_op(SESSION, MCP.name, {"amount": 9}, "charged")
    monkeypatch.setitem(gate_mod.HIGH_RISK_GUARD, "enabled", False)
    assert _check(store, args={"amount": 9}).refusal is None
    assert replay_path_gate_enabled() is False


def test_the_replay_path_switch_only_gates_the_new_paths(monkeypatch) -> None:
    monkeypatch.setitem(gate_mod.HIGH_RISK_GUARD, "replay_path_gate_enabled", False)
    assert replay_path_gate_enabled() is False
    # The middleware's guard is unaffected (it reads ``enabled`` only):
    assert gate_mod.guard_enabled() is True
