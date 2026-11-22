"""Subagent access-mode inheritance, approval scope and approval-context stamping."""

from __future__ import annotations

import pytest

from agent.tools.subagent.registry.session_keys import normalize_session_key
from agent.tools.subagent.spawn.child_agent_factory import _bind_child_run_context
from agent.tools.subagent.spawn.core import _stamp_approval_context
from agent.tools.subagent.spawn.request import SpawnRequest
from agent.tools.subagent.types.registry import SubagentRunRecord
from runtime import StateKey, state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _run(root: str, child: str) -> SubagentRunRecord:
    return SubagentRunRecord(
        run_id=f"run-{child}",
        child_session_key=child,
        requester_session_key=root,
        task="inheritance probe",
    )


def test_the_spawn_request_carries_an_approval_context():
    request = SpawnRequest.from_legacy(
        task="t",
        requester_session_key="sess",
        approval_context={"keyword": "payment", "tool": "terminal"},
    )
    assert request.approval_context == {"keyword": "payment", "tool": "terminal"}
    # Omitted keeps the legacy default (no approval).
    assert SpawnRequest.from_legacy(task="t", requester_session_key="s").approval_context is None


def test_full_access_is_inherited_and_scopes_approvals_to_the_root():
    root = "sess-inherit-yolo"
    child = "agent:main:subagent:inherit-yolo"
    state_register_mem.set_state(root, StateKey.HITL_SESSION_YOLO, True)

    _bind_child_run_context(_run(root, child))

    scope = normalize_session_key(root)
    assert state_register_mem.get_state(child, StateKey.APPROVAL_SCOPE_PARENT, "") == scope
    assert state_register_mem.get_state(child, StateKey.HITL_SESSION_YOLO, False) is True
    assert state_register_mem.get_state(child, StateKey.HITL_SESSION_CONFIRM_ALL, False) is False


def test_confirm_all_is_inherited_without_yolo():
    root = "sess-inherit-confirm"
    child = "agent:main:subagent:inherit-confirm"
    state_register_mem.set_state(root, StateKey.HITL_SESSION_CONFIRM_ALL, True)

    _bind_child_run_context(_run(root, child))

    assert state_register_mem.get_state(child, StateKey.HITL_SESSION_CONFIRM_ALL, False) is True
    assert state_register_mem.get_state(child, StateKey.HITL_SESSION_YOLO, False) is False


def test_auto_edit_sets_neither_flag():
    root = "sess-inherit-auto"
    child = "agent:main:subagent:inherit-auto"

    _bind_child_run_context(_run(root, child))

    assert state_register_mem.get_state(child, StateKey.HITL_SESSION_YOLO, False) is False
    assert state_register_mem.get_state(child, StateKey.HITL_SESSION_CONFIRM_ALL, False) is False


def test_the_scope_walks_to_the_token_budget_root():
    parent = "sess-inherit-parent"
    main_root = "sess-inherit-main-root"
    child = "agent:main:subagent:inherit-nested"
    state_register_mem.set_state(
        parent, StateKey.TOKEN_BUDGET_PARENT, {"session_id": main_root, "turn_id": "t1"}
    )
    state_register_mem.set_state(main_root, StateKey.HITL_SESSION_YOLO, True)

    _bind_child_run_context(_run(parent, child))

    assert state_register_mem.get_state(child, StateKey.APPROVAL_SCOPE_PARENT, "") == main_root
    assert state_register_mem.get_state(child, StateKey.HITL_SESSION_YOLO, False) is True


def test_approval_context_is_stamped_on_the_child_only():
    child = "agent:main:subagent:stamped"
    _stamp_approval_context(child, {"keyword": "payment", "tool": "terminal", "args_hash": "abc"})
    approvals = state_register_mem.get_state(child, StateKey.KEYWORD_APPROVALS, [])
    assert approvals == [{"keyword": "payment", "tool": "terminal", "args_hash": "abc"}]

    # A missing/blank context writes nothing.
    empty_child = "agent:main:subagent:stamped-empty"
    _stamp_approval_context(empty_child, None)
    _stamp_approval_context(empty_child, {"keyword": "   "})
    assert state_register_mem.get_state(empty_child, StateKey.KEYWORD_APPROVALS, []) == []
