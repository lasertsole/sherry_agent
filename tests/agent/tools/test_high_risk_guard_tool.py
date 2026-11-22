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
