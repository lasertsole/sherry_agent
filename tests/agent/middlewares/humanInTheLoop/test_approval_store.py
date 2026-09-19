"""ToolApprovalStore unit tests (P2-2): persistence, CAS, operator scope, auto-deny."""

from __future__ import annotations

import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from agent.middlewares.humanInTheLoop.approval_scope import (
    NO_OPERATOR_MESSAGE,
    operator_scope,
    resolve_turn_operator,
)
from agent.middlewares.humanInTheLoop.approval_store import (
    ApprovalVerdict,
    ToolApprovalStore,
)

pytestmark = pytest.mark.unit

_ARGS = {"command": "git reset --hard"}
_OTHER_ARGS = {"command": "ls -la"}


def _store(tmp_path: Path) -> ToolApprovalStore:
    return ToolApprovalStore(tmp_path / "approvals.json")


# ────────────────────────────────────────────────────────────────────────────
# persistence round-trip
# ────────────────────────────────────────────────────────────────────────────


def test_recorded_decisions_survive_a_new_instance(tmp_path):
    path = tmp_path / "approvals.json"
    first = ToolApprovalStore(path)
    with operator_scope("alice"):
        assert first.record("terminal", _ARGS, "s1", allow=True, reason="approved once")
        assert first.record("python_repl", _OTHER_ARGS, "s1", allow=False, reason="User denied: no")

    restarted = ToolApprovalStore(path)
    with operator_scope("alice"):
        allowed = restarted.evaluate("terminal", _ARGS, "s1")
        denied = restarted.evaluate("python_repl", _OTHER_ARGS, "s1")
    assert allowed.verdict is ApprovalVerdict.ALLOW
    assert allowed.reason == "approved once"
    assert denied.verdict is ApprovalVerdict.DENY
    assert denied.reason == "User denied: no"


def test_missing_file_is_an_empty_policy(tmp_path):
    path = tmp_path / "approvals.json"
    store = ToolApprovalStore(path)
    with operator_scope("alice"):
        evaluation = store.evaluate("terminal", _ARGS, "s1")
    assert evaluation.verdict is ApprovalVerdict.UNKNOWN
    assert evaluation.auto_denied is False
    assert not path.exists(), "a read must never create the store file"


def test_different_args_do_not_reuse_a_decision(tmp_path):
    store = _store(tmp_path)
    with operator_scope("alice"):
        store.record("terminal", _ARGS, "s1", allow=True)
        same = store.evaluate("terminal", _ARGS, "s1")
        other = store.evaluate("terminal", _OTHER_ARGS, "s1")
    assert same.verdict is ApprovalVerdict.ALLOW
    assert other.verdict is ApprovalVerdict.UNKNOWN


def test_raw_tool_args_are_never_persisted(tmp_path):
    path = tmp_path / "approvals.json"
    store = ToolApprovalStore(path)
    secret_args = {"command": "curl -H 'Authorization: Bearer TOP-SECRET-TOKEN'"}
    with operator_scope("alice"):
        assert store.record("terminal", secret_args, "s1", allow=True)
    raw = path.read_text(encoding="utf-8")
    assert "TOP-SECRET-TOKEN" not in raw
    assert secret_args["command"] not in raw


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not enforced on Windows")
def test_store_file_is_private(tmp_path):
    path = tmp_path / "approvals.json"
    store = ToolApprovalStore(path)
    with operator_scope("alice"):
        store.record("terminal", _ARGS, "s1", allow=True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ────────────────────────────────────────────────────────────────────────────
# byte-revision CAS
# ────────────────────────────────────────────────────────────────────────────


def test_write_if_revision_rejects_stale_bytes(tmp_path):
    path = tmp_path / "approvals.json"
    writer = ToolApprovalStore(path)
    with operator_scope("alice"):
        assert writer.record("tool_a", {"a": 1}, "s1", allow=True)

    reader = ToolApprovalStore(path)
    snapshot = reader._read_snapshot()
    with operator_scope("alice"):
        assert writer.record("tool_b", {"b": 2}, "s1", allow=True)

    # A write carrying the pre-update revision must lose.
    stale = dict(snapshot.data)
    assert reader._write_if_revision(stale, snapshot.revision) is False


def test_record_retries_after_a_cas_conflict(tmp_path, monkeypatch):
    path = tmp_path / "approvals.json"
    store = ToolApprovalStore(path)
    other = ToolApprovalStore(path)
    with operator_scope("alice"):
        assert other.record("tool_a", {"a": 1}, "s1", allow=True)

    stale = store._read_snapshot()
    with operator_scope("alice"):
        assert other.record("tool_moved_on", {"moved": 1}, "s1", allow=True)

    real_read = store._read_snapshot
    calls = {"n": 0}

    def stale_first_read():
        calls["n"] += 1
        return stale if calls["n"] == 1 else real_read()

    monkeypatch.setattr(store, "_read_snapshot", stale_first_read)
    with operator_scope("alice"):
        assert store.record("tool_b", {"b": 2}, "s1", allow=True)
    assert calls["n"] == 2
    assert store.cas_conflicts == 1
    with operator_scope("alice"):
        assert other.evaluate("tool_a", {"a": 1}, "s1").verdict is ApprovalVerdict.ALLOW
        assert other.evaluate("tool_moved_on", {"moved": 1}, "s1").verdict is ApprovalVerdict.ALLOW
        assert other.evaluate("tool_b", {"b": 2}, "s1").verdict is ApprovalVerdict.ALLOW


def test_record_fails_after_exhausting_retries(tmp_path, monkeypatch):
    path = tmp_path / "approvals.json"
    store = ToolApprovalStore(path, max_cas_retries=2)
    stale = store._read_snapshot()
    with operator_scope("alice"):
        assert store.record("tool_a", {"a": 1}, "s1", allow=True)
    monkeypatch.setattr(store, "_read_snapshot", lambda: stale)
    with operator_scope("alice"):
        assert store.record("tool_b", {"b": 2}, "s1", allow=True) is False
    assert store.cas_conflicts == 2


def test_concurrent_writers_lose_no_decisions(tmp_path):
    path = tmp_path / "approvals.json"
    stores = [ToolApprovalStore(path), ToolApprovalStore(path)]
    barrier = threading.Barrier(2)

    def _write(index: int) -> None:
        barrier.wait()
        for item in range(5):
            with operator_scope("alice"):
                assert stores[index].record(f"tool_{index}", {"item": item}, "s1", allow=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(_write, range(2)))

    reader = ToolApprovalStore(path)
    with operator_scope("alice"):
        for index in range(2):
            for item in range(5):
                evaluation = reader.evaluate(f"tool_{index}", {"item": item}, "s1")
                assert evaluation.verdict is ApprovalVerdict.ALLOW


def test_corrupt_file_reads_as_empty_and_recovers_on_write(tmp_path):
    path = tmp_path / "approvals.json"
    path.write_text("{ not json", encoding="utf-8")
    store = ToolApprovalStore(path)
    with operator_scope("alice"):
        assert store.evaluate("terminal", _ARGS, "s1").verdict is ApprovalVerdict.UNKNOWN
        assert store.record("terminal", _ARGS, "s1", allow=True)
    restarted = ToolApprovalStore(path)
    with operator_scope("alice"):
        assert restarted.evaluate("terminal", _ARGS, "s1").verdict is ApprovalVerdict.ALLOW


# ────────────────────────────────────────────────────────────────────────────
# operator scope + auto-deny
# ────────────────────────────────────────────────────────────────────────────


def test_operator_decisions_are_isolated(tmp_path):
    store = _store(tmp_path)
    with operator_scope("alice"):
        assert store.record("terminal", _ARGS, "s1", allow=True)
        assert store.evaluate("terminal", _ARGS, "s1").verdict is ApprovalVerdict.ALLOW
    with operator_scope("bob"):
        assert store.evaluate("terminal", _ARGS, "s1").verdict is ApprovalVerdict.UNKNOWN
    with operator_scope("alice"):
        assert store.evaluate("terminal", _ARGS, "s1").verdict is ApprovalVerdict.ALLOW


def test_sessions_are_isolated_within_one_operator(tmp_path):
    store = _store(tmp_path)
    with operator_scope("alice"):
        store.record("terminal", _ARGS, "s1", allow=True)
        assert store.evaluate("terminal", _ARGS, "s1").verdict is ApprovalVerdict.ALLOW
        assert store.evaluate("terminal", _ARGS, "s2").verdict is ApprovalVerdict.UNKNOWN


def test_no_operator_auto_denies_and_refuses_to_record(tmp_path):
    path = tmp_path / "approvals.json"
    store = ToolApprovalStore(path)
    evaluation = store.evaluate("terminal", _ARGS, "s1")
    assert evaluation.verdict is ApprovalVerdict.DENY
    assert evaluation.auto_denied is True
    assert NO_OPERATOR_MESSAGE in evaluation.reason
    assert store.record("terminal", _ARGS, "s1", allow=True) is False
    assert not path.exists()


def test_explicit_none_scope_beats_the_session_fallback(tmp_path):
    store = _store(tmp_path)
    with operator_scope(None):
        assert store.evaluate("terminal", _ARGS, "s1").auto_denied is True


def test_resolve_turn_operator_prefers_scope_then_checks_headless(tmp_path):
    human_state = {"messages": [HumanMessage(content="run it")]}
    cron_state = {
        "messages": [
            HumanMessage(content="scheduled", metadata={"origin": "cron", "internal": True})
        ]
    }
    internal_state = {"messages": [HumanMessage(content="carrier", metadata={"internal": True})]}
    assert resolve_turn_operator(human_state, "s1") == "s1"
    assert resolve_turn_operator(cron_state, "s1") is None
    assert resolve_turn_operator(internal_state, "s1") is None
    assert resolve_turn_operator(cron_state, "s1") is None
    with operator_scope("alice"):
        assert resolve_turn_operator(cron_state, "s1") == "alice"


def test_store_document_shape(tmp_path):
    path = tmp_path / "approvals.json"
    store = ToolApprovalStore(path)
    with operator_scope("alice"):
        store.record("terminal", _ARGS, "s1", allow=True, reason="ok")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["version"] == 1
    assert document["revision"] == 1
    assert document["approvals"]["alice"]["s1"]["terminal"]
