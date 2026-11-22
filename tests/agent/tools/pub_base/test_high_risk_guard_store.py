"""High-risk guard store: keyword registry, scanning, execution ledger."""

from __future__ import annotations

import sqlite3

import pytest

from agent.tools.pub_base.high_risk_guard_store import (
    HighRiskGuardStore,
    args_hash,
    scan_high_risk_keywords,
    validate_keyword,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "sess-guard"


@pytest.fixture()
def store(tmp_path) -> HighRiskGuardStore:
    return HighRiskGuardStore(path=str(tmp_path / "guard.db"))


# --------------------------------------------------------------------------
# Validation + scanning
# --------------------------------------------------------------------------


def test_keyword_length_is_counted_in_characters():
    assert validate_keyword("ab") is not None
    assert validate_keyword("密码") is not None  # 2 characters, refused
    assert validate_keyword("api") is None
    assert validate_keyword("银行卡") is None  # 3 characters, accepted
    assert validate_keyword("   ") is not None
    assert validate_keyword("x" * 101) is not None


def test_scan_is_case_insensitive_and_substring_based():
    hit = scan_high_risk_keywords("curl -d user_password=secret", ["password"])
    assert hit is not None and hit.keyword == "password"
    assert "password" in hit.context
    assert scan_high_risk_keywords("curl -d user=1", ["password"]) is None


def test_scan_returns_the_first_listed_keyword():
    hit = scan_high_risk_keywords("DELETE FROM users", ["DROP TABLE", "DELETE FROM"])
    assert hit is not None and hit.keyword == "DELETE FROM"


def test_args_hash_ignores_key_order():
    assert args_hash("t", {"a": 1, "b": 2}) == args_hash("t", {"b": 2, "a": 1})
    assert args_hash("t", {"a": 1}) != args_hash("u", {"a": 1})
    assert args_hash("t", {"a": 1}) != args_hash("t", {"a": 2})


# --------------------------------------------------------------------------
# Keyword registry
# --------------------------------------------------------------------------


def test_builtins_are_blocked_by_default(store):
    blockers = store.load_active_blockers(SESSION)
    assert "rm -rf" in blockers
    assert "password" in blockers


def test_add_keyword_is_session_scoped_and_blocks(store):
    ok, _ = store.add_keyword(SESSION, "deploy_prod")
    assert ok
    assert "deploy_prod" in store.load_active_blockers(SESSION)
    assert "deploy_prod" not in store.load_active_blockers("other-session")
    listed = {entry["keyword"]: entry for entry in store.list_keywords(SESSION)}
    assert listed["deploy_prod"]["policy"] == "block"
    assert listed["deploy_prod"]["source"] == "agent"


def test_a_global_keyword_reaches_every_session(store):
    store.add_keyword(SESSION, "publish", scope="global")
    assert "publish" in store.load_active_blockers("another-session")


def test_allow_removes_a_keyword_from_the_blockers(store):
    store.add_keyword(SESSION, "password")
    assert "password" in store.load_active_blockers(SESSION)
    ok, _ = store.set_policy(SESSION, "password", "allow")
    assert ok
    assert "password" not in store.load_active_blockers(SESSION)
    # A built-in override row also works.
    store.set_policy(SESSION, "rm -rf", "allow")
    assert "rm -rf" not in store.load_active_blockers(SESSION)


def test_short_keywords_are_refused(store):
    ok, message = store.add_keyword(SESSION, "ab")
    assert not ok and "3" in message
    assert "ab" not in store.load_active_blockers(SESSION)


def test_remove_keyword_by_id_and_by_name(store):
    store.add_keyword(SESSION, "alpha")
    store.add_keyword(SESSION, "beta")
    entry = next(row for row in store.list_keywords(SESSION) if row["keyword"] == "alpha")
    assert store.remove_keyword(SESSION, entry["id"])[0] is True
    assert store.remove_keyword(SESSION, "beta")[0] is True
    assert "alpha" not in store.load_active_blockers(SESSION)
    assert "beta" not in store.load_active_blockers(SESSION)


def test_removing_a_builtin_override_restores_the_default(store):
    store.set_policy(SESSION, "password", "allow")
    entry = next(row for row in store.list_keywords(SESSION) if row["keyword"] == "password")
    assert entry["id"] is not None
    assert store.remove_keyword(SESSION, entry["id"])[0] is True
    assert "password" in store.load_active_blockers(SESSION)


def test_mark_executed_and_reset(store):
    store.mark_executed(SESSION, "deploy", "deployed to prod")
    entry = store.get_keyword(SESSION, "deploy")
    assert entry is not None and entry["executed"] is True
    assert entry["result_summary"] == "deployed to prod"
    assert store.reset_executed(SESSION, "deploy") is True
    assert store.get_keyword(SESSION, "deploy")["executed"] is False


def test_executed_state_does_not_leak_across_sessions(store):
    store.mark_executed(SESSION, "deploy", "done")
    other = store.get_keyword("other-session", "deploy")
    assert other is None  # a custom keyword is session-scoped


def test_purge_session_keeps_global_keywords(store):
    store.add_keyword(SESSION, "local-kw")
    store.add_keyword(SESSION, "global-kw", scope="global")
    store.record_op(SESSION, "mcp__x__charge", {"amount": 1}, "ok")
    store.purge_session(SESSION)
    assert "local-kw" not in store.load_active_blockers(SESSION)
    assert "global-kw" in store.load_active_blockers(SESSION)
    assert store.list_ops(SESSION) == []


# --------------------------------------------------------------------------
# Execution ledger
# --------------------------------------------------------------------------


def test_ledger_round_trip(store):
    assert store.check_op(SESSION, "mcp__stripe__charge", {"amount": 100}) is None
    store.record_op(SESSION, "mcp__stripe__charge", {"amount": 100}, "charged")
    hit = store.check_op(SESSION, "mcp__stripe__charge", {"amount": 100})
    assert hit is not None and hit["result_summary"] == "charged"
    # A different argument set is a different operation.
    assert store.check_op(SESSION, "mcp__stripe__charge", {"amount": 200}) is None
    # Another session does not see it.
    assert store.check_op("other", "mcp__stripe__charge", {"amount": 100}) is None


def test_ledger_record_is_idempotent(store):
    args = {"amount": 100}
    store.record_op(SESSION, "mcp__stripe__charge", args, "first")
    store.record_op(SESSION, "mcp__stripe__charge", args, "second")
    rows = store.list_ops(SESSION)
    assert len(rows) == 1 and rows[0]["result_summary"] == "second"


def test_ledger_remove_allows_a_rerun(store):
    store.record_op(SESSION, "execute_code", {"script": "deploy.py"}, "deployed")
    entry = store.list_ops(SESSION)[0]
    assert store.check_op(SESSION, "execute_code", {"script": "deploy.py"}) is not None
    assert store.remove_op(SESSION, entry["id"]) is True
    assert store.check_op(SESSION, "execute_code", {"script": "deploy.py"}) is None


def test_ledger_preview_is_bounded_and_single_line(store):
    store.record_op(SESSION, "t", {"body": "x" * 1000 + "\nnext"}, "ok")
    row = store.list_ops(SESSION)[0]
    assert len(row["args_preview"]) <= 300
    assert "\n" not in row["args_preview"]


def test_retention_expires_old_ops(tmp_path):
    path = tmp_path / "guard.db"
    store = HighRiskGuardStore(path=str(path))
    store.record_op(SESSION, "t", {"a": 1}, "ok")
    with sqlite3.connect(str(path)) as conn:
        conn.execute("UPDATE high_risk_ops SET executed_at = 0")
        conn.commit()
    store._last_purge = None  # force the next access to sweep
    store.list_keywords(SESSION)
    assert store.list_ops(SESSION) == []
