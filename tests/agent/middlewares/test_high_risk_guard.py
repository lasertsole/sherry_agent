"""ToolGuardrails high-risk gate: keyword scan + executed-operation ledger.

The module is loaded by absolute path (same iron rule as
``test_tool_guardrails.py``): the subagent conftest installs permanent
``sys.modules`` stubs that would shadow the real module.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
MODULE_PATH = REPO_ROOT / "agent" / "middlewares" / "tool_guardrails" / "core.py"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location("tool_guardrails_high_risk", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules["tool_guardrails_high_risk"] = mod
_spec.loader.exec_module(mod)

from agent.tools.pub_base.high_risk_guard_store import HighRiskGuardStore  # noqa: E402
from runtime import StateKey, state_register_mem  # noqa: E402

import pytest  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

MCP_TOOL = SimpleNamespace(
    name="mcp__stripe__charge",
    metadata={"high_risk": True, "idempotent": False, "side_effect_scope": "system"},
)
TERMINAL_TOOL = SimpleNamespace(
    name="terminal",
    metadata={"high_risk": False, "scan_keywords": True, "idempotent": False},
)
WRITE_TOOL = SimpleNamespace(name="write_file", metadata={"high_risk": False, "idempotent": False})


def _mw() -> mod.ToolGuardrails:
    return mod.ToolGuardrails(mod.ToolCallGuardrailConfig())


def _request(session: str, tool, args: dict, call_id: str = "c1") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": tool.name, "args": args, "id": call_id},
        tool=tool,
        state={"session_id": session},
        runtime=None,
    )


def _run(mw, request, *, content="ok", status="success") -> tuple[ToolMessage, list[str]]:
    calls: list[str] = []

    async def handler(req):
        calls.append(str(req.tool_call["name"]))
        return ToolMessage(
            content=content,
            tool_call_id=str(req.tool_call["id"]),
            name=str(req.tool_call["name"]),
            status=status,
        )

    return asyncio.run(mw.awrap_tool_call(request, handler)), calls


@pytest.fixture()
def guard_db(tmp_path, monkeypatch):
    path = tmp_path / "guard.db"
    monkeypatch.setenv("SHERRY_HIGH_RISK_GUARD_PATH", str(path))
    return HighRiskGuardStore(path=str(path))


def test_an_unannotated_builtin_is_never_gated(guard_db):
    mw = _mw()
    result, calls = _run(mw, _request("hr-write", WRITE_TOOL, {"path": "a.py"}))
    assert calls == ["write_file"]
    assert result.content == "ok"


def test_a_high_risk_call_is_ledgered_and_a_repeat_is_blocked(guard_db):
    mw = _mw()
    session = "hr-mcp"
    first, calls = _run(mw, _request(session, MCP_TOOL, {"amount": 100}), content="charged")
    assert calls == ["mcp__stripe__charge"]
    assert "charged" in first.content

    second, calls = _run(mw, _request(session, MCP_TOOL, {"amount": 100}, "c2"))
    assert calls == [], "the repeated operation must not execute"
    assert "already ran" in second.content and "charged" in second.content

    other, calls = _run(mw, _request(session, MCP_TOOL, {"amount": 200}, "c3"))
    assert calls == ["mcp__stripe__charge"], "different args are a different operation"
    assert "already ran" not in other.content


def test_a_failed_high_risk_call_is_not_ledgered(guard_db):
    mw = _mw()
    session = "hr-mcp-fail"
    _run(mw, _request(session, MCP_TOOL, {"amount": 1}), content="boom", status="error")
    _, calls = _run(mw, _request(session, MCP_TOOL, {"amount": 1}, "c2"))
    assert calls == ["mcp__stripe__charge"], "a failure may be retried"


def test_a_keyword_hit_returns_needs_approval_without_executing(guard_db):
    mw = _mw()
    result, calls = _run(mw, _request("hr-kw", TERMINAL_TOOL, {"commands": ["rm -rf /tmp/x"]}))
    assert calls == []
    assert "NEEDS_APPROVAL" in result.content and "rm -rf" in result.content
    assert result.status == "error"


def test_an_approval_lets_the_call_run_then_marks_the_keyword_executed(guard_db):
    mw = _mw()
    session = "hr-kw-approved"
    state_register_mem.set_state(
        session,
        StateKey.KEYWORD_APPROVALS,
        [{"keyword": "rm -rf", "tool": "terminal"}],
    )
    result, calls = _run(
        mw, _request(session, TERMINAL_TOOL, {"commands": ["rm -rf build"]}), content="removed"
    )
    assert calls == ["terminal"]
    assert "NEEDS_APPROVAL" not in result.content
    entry = guard_db.get_keyword(session, "rm -rf")
    assert entry is not None and entry["executed"] is True
    assert entry["result_summary"] == "removed"

    # The marker outranks the still-recorded approval: a repeat is refused.
    repeat, calls = _run(
        mw,
        _request(session, TERMINAL_TOOL, {"commands": ["rm -rf dist"]}, "c2"),
    )
    assert calls == []
    assert "already ran" in repeat.content


def test_an_args_scoped_approval_only_covers_that_call(guard_db):
    mw = _mw()
    session = "hr-kw-args"
    from agent.tools.pub_base.high_risk_guard_store import args_hash

    digest = args_hash("terminal", {"commands": ["echo password-reset"]})
    state_register_mem.set_state(
        session,
        StateKey.KEYWORD_APPROVALS,
        [{"keyword": "password", "tool": "terminal", "args_hash": digest}],
    )
    approved, calls = _run(
        mw, _request(session, TERMINAL_TOOL, {"commands": ["echo password-reset"]})
    )
    assert calls == ["terminal"] and "NEEDS_APPROVAL" not in approved.content

    guard_db.reset_executed(session, "password")
    other, calls = _run(
        mw, _request(session, TERMINAL_TOOL, {"commands": ["echo password-other"]}, "c2")
    )
    assert calls == [] and "NEEDS_APPROVAL" in other.content


def test_yolo_skips_the_keyword_card_for_a_reversible_tool(guard_db):
    mw = _mw()
    session = "hr-yolo"
    state_register_mem.set_state(session, StateKey.HITL_SESSION_YOLO, True)
    _, calls = _run(mw, _request(session, TERMINAL_TOOL, {"commands": ["echo password"]}))
    assert calls == ["terminal"]


def test_confirm_all_refuses_a_childs_keyword_hit(guard_db):
    mw = _mw()
    child = "agent:main:subagent:hr-confirm"
    state_register_mem.set_state(child, StateKey.HITL_SESSION_CONFIRM_ALL, True)
    result, calls = _run(mw, _request(child, TERMINAL_TOOL, {"commands": ["echo password"]}))
    assert calls == []
    assert "confirm_all" in result.content


def test_a_child_shares_the_root_ledger(guard_db):
    mw = _mw()
    root = "hr-root"
    child = "agent:main:subagent:hr-child"
    state_register_mem.set_state(child, StateKey.APPROVAL_SCOPE_PARENT, root)
    first, calls = _run(mw, _request(child, MCP_TOOL, {"amount": 5}), content="charged")
    assert calls == ["mcp__stripe__charge"]
    assert guard_db.list_ops(root), "the child's execution lands in the root ledger"

    second, calls = _run(mw, _request(child, MCP_TOOL, {"amount": 5}, "c2"))
    assert calls == [] and "already ran" in second.content


def test_a_yolo_child_still_hits_the_root_ledger(guard_db):
    mw = _mw()
    root = "hr-root-yolo"
    child = "agent:main:subagent:hr-child-yolo"
    state_register_mem.set_state(child, StateKey.APPROVAL_SCOPE_PARENT, root)
    state_register_mem.set_state(child, StateKey.HITL_SESSION_YOLO, True)
    _, calls = _run(
        mw,
        _request(child, TERMINAL_TOOL, {"commands": ["echo password"]}),
    )
    assert calls == ["terminal"], "full_access trusts the child's reversible call"

    blocked, calls = _run(
        mw,
        _request(child, TERMINAL_TOOL, {"commands": ["echo password"]}, "c2"),
    )
    # The keyword executed marker (session/root scope) blocks the second one.
    assert calls == [] and "already ran" in blocked.content
