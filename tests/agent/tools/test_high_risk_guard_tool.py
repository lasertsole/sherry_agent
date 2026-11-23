"""The agent-facing high_risk_guard tool: keyword + ledger management."""

from __future__ import annotations

import pytest

from agent.tools.high_risk_guard import build_high_risk_guard_tool
from agent.tools.pub_base.high_risk_guard_store import default_store
from runtime import StateKey, state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "sess-guard-tool"


@pytest.fixture(autouse=True)
def _guard_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SHERRY_HIGH_RISK_GUARD_PATH", str(tmp_path / "guard.db"))


def test_add_list_and_remove_a_keyword():
    tool = build_high_risk_guard_tool()
    assert tool.metadata["high_risk"] is False

    added = tool.invoke({"action": "add_keyword", "keyword": "deploy_prod", "session_id": SESSION})
    assert "registered" in added, added
    assert "deploy_prod" in default_store().load_active_blockers(SESSION)

    listed = tool.invoke({"action": "list_keywords", "session_id": SESSION})
    assert "deploy_prod" in listed and "rm -rf" in listed

    removed = tool.invoke(
        {"action": "remove_keyword", "keyword": "deploy_prod", "session_id": SESSION}
    )
    assert "removed" in removed
    assert "deploy_prod" not in default_store().load_active_blockers(SESSION)


def test_short_keywords_are_refused_by_the_tool():
    tool = build_high_risk_guard_tool()
    out = tool.invoke({"action": "add_keyword", "keyword": "ab", "session_id": SESSION})
    assert out.startswith("Error:")


def test_set_policy_and_reset():
    tool = build_high_risk_guard_tool()
    tool.invoke({"action": "add_keyword", "keyword": "deploy", "session_id": SESSION})
    out = tool.invoke(
        {"action": "set_policy", "keyword": "deploy", "policy": "allow", "session_id": SESSION}
    )
    assert "policy=allow" in out
    assert "deploy" not in default_store().load_active_blockers(SESSION)

    default_store().mark_executed(SESSION, "deploy", "done")
    out = tool.invoke({"action": "reset_keyword", "keyword": "deploy", "session_id": SESSION})
    assert "reset" in out
    assert default_store().get_keyword(SESSION, "deploy")["executed"] is False


def test_register_list_and_remove_an_operation():
    tool = build_high_risk_guard_tool()
    out = tool.invoke(
        {
            "action": "register_op",
            "tool_name": "execute_code",
            "args": {"script": "deploy.py"},
            "result_summary": "deployed",
            "session_id": SESSION,
        }
    )
    assert "operation registered" in out
    assert default_store().check_op(SESSION, "execute_code", {"script": "deploy.py"})

    listed = tool.invoke({"action": "list_ops", "session_id": SESSION})
    assert "execute_code" in listed
    op_id = default_store().list_ops(SESSION)[0]["id"]
    removed = tool.invoke({"action": "remove_op", "op_id": op_id, "session_id": SESSION})
    assert "removed" in removed


def test_a_child_registers_into_the_root_scope():
    child = "agent:main:subagent:guard-tool"
    state_register_mem.set_state(child, StateKey.APPROVAL_SCOPE_PARENT, SESSION)
    tool = build_high_risk_guard_tool()
    tool.invoke({"action": "add_keyword", "keyword": "from_child", "session_id": child})
    assert "from_child" in default_store().load_active_blockers(SESSION)


def test_unknown_action_is_reported():
    tool = build_high_risk_guard_tool()
    # Pydantic's Literal rejects it before the body; a dict through invoke raises
    # a validation error rather than executing anything.
    with pytest.raises(Exception):
        tool.invoke({"action": "nope", "session_id": SESSION})


# ---------------------------------------------------------------------------
# A subagent may tighten the guard but never loosen it
# ---------------------------------------------------------------------------

CHILD = "agent:main:subagent:guard-permissions"


def _as_child() -> None:
    state_register_mem.set_state(CHILD, StateKey.APPROVAL_SCOPE_PARENT, SESSION)


def test_a_child_cannot_switch_a_keyword_to_allow():
    tool = build_high_risk_guard_tool()
    tool.invoke({"action": "add_keyword", "keyword": "deploy_prod", "session_id": SESSION})
    _as_child()

    out = tool.invoke(
        {
            "action": "set_policy",
            "keyword": "deploy_prod",
            "policy": "allow",
            "session_id": CHILD,
        }
    )

    assert out.startswith("Error:")
    assert "deploy_prod" in default_store().load_active_blockers(SESSION)


def test_a_child_cannot_register_an_allow_keyword():
    tool = build_high_risk_guard_tool()
    _as_child()

    out = tool.invoke(
        {
            "action": "add_keyword",
            "keyword": "charge_card",
            "policy": "allow",
            "session_id": CHILD,
        }
    )

    assert out.startswith("Error:")
    assert default_store().get_keyword(SESSION, "charge_card") is None


def test_a_child_can_tighten():
    tool = build_high_risk_guard_tool()
    _as_child()

    added = tool.invoke({"action": "add_keyword", "keyword": "deploy_prod", "session_id": CHILD})

    assert "registered" in added
    assert "deploy_prod" in default_store().load_active_blockers(SESSION)


def test_a_child_cannot_remove_a_block_keyword():
    tool = build_high_risk_guard_tool()
    tool.invoke({"action": "add_keyword", "keyword": "deploy_prod", "session_id": SESSION})
    _as_child()

    out = tool.invoke({"action": "remove_keyword", "keyword": "deploy_prod", "session_id": CHILD})

    assert out.startswith("Error:")
    assert "deploy_prod" in default_store().load_active_blockers(SESSION)


def test_a_child_cannot_remove_a_builtin_default():
    tool = build_high_risk_guard_tool()
    _as_child()

    out = tool.invoke({"action": "remove_keyword", "keyword": "rm -rf", "session_id": CHILD})

    assert out.startswith("Error:")
    assert "rm -rf" in default_store().load_active_blockers(SESSION)


def test_a_child_can_remove_an_allow_override():
    tool = build_high_risk_guard_tool()
    tool.invoke(
        {"action": "set_policy", "keyword": "password", "policy": "allow", "session_id": SESSION}
    )
    _as_child()

    out = tool.invoke({"action": "remove_keyword", "keyword": "password", "session_id": CHILD})

    assert "removed" in out
    assert "password" in default_store().load_active_blockers(SESSION)


def test_a_child_may_reset_only_a_keyword_it_was_approved_for():
    tool = build_high_risk_guard_tool()
    default_store().mark_executed(SESSION, "deploy", "done")
    _as_child()

    refused = tool.invoke({"action": "reset_keyword", "keyword": "deploy", "session_id": CHILD})
    assert refused.startswith("Error:")
    assert default_store().get_keyword(SESSION, "deploy")["executed"] is True

    state_register_mem.set_state(
        CHILD, StateKey.KEYWORD_APPROVALS, [{"keyword": "deploy", "tool": "terminal"}]
    )
    allowed = tool.invoke({"action": "reset_keyword", "keyword": "deploy", "session_id": CHILD})
    assert "reset" in allowed
    assert default_store().get_keyword(SESSION, "deploy")["executed"] is False


def test_a_child_cannot_delete_a_ledger_row():
    tool = build_high_risk_guard_tool()
    tool.invoke(
        {
            "action": "register_op",
            "tool_name": "execute_code",
            "args": {"script": "deploy.py"},
            "session_id": SESSION,
        }
    )
    op_id = default_store().list_ops(SESSION)[0]["id"]
    _as_child()

    out = tool.invoke({"action": "remove_op", "op_id": op_id, "session_id": CHILD})

    assert out.startswith("Error:")
    assert default_store().check_op(SESSION, "execute_code", {"script": "deploy.py"})
