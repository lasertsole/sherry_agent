"""Session command cache: dangerous commands always bypass it."""

from __future__ import annotations

import pytest

from agent.middlewares.humanInTheLoop.approval import ApprovalPipeline
from agent.middlewares.humanInTheLoop.types import (
    HITL_SESSION_APPROVED_KEY,
    ApprovalDecision,
    HITLConfig,
)
from runtime.session.state_register import state_register_mem

pytestmark = [pytest.mark.unit]


@pytest.fixture()
def pipeline():
    return ApprovalPipeline(HITLConfig(), lambda *_args, **_kwargs: None)


def _seed(session: str, pattern: str) -> None:
    state_register_mem.set_state(session, HITL_SESSION_APPROVED_KEY, [pattern])


def test_a_dangerous_command_skips_a_matching_session_pattern(pipeline):
    session = "cmd-cache-dangerous"
    _seed(session, "rm -rf*")

    result = pipeline.check_command("rm -rf build/", session)

    assert result.approved is False
    assert result.decision is None
    assert "Dangerous" in result.reason


def test_a_safe_command_uses_a_matching_session_pattern(pipeline):
    session = "cmd-cache-safe"
    _seed(session, "npm install*")

    result = pipeline.check_command("npm install --save-dev lodash", session)

    assert result.approved is True
    assert result.decision == ApprovalDecision.SESSION


def test_a_dangerous_command_does_not_use_a_permanent_allowlist(pipeline):
    """Permanent allowlist remains the user's explicit ALWAYS decision."""
    from agent.middlewares.humanInTheLoop.types import HITL_PERMANENT_KEY

    session = "cmd-cache-permanent"
    state_register_mem.set_state(session, HITL_PERMANENT_KEY, ["rm -rf*"])

    result = pipeline.check_command("rm -rf build/", session)

    assert result.approved is True
    assert result.decision == ApprovalDecision.ALWAYS


class _FakeApproval:
    def __init__(self, approved: bool = False):
        self.approved = approved
        self.applied: list[tuple] = []

    def check_command(self, command, session_id):
        from agent.middlewares.humanInTheLoop.types import ApprovalResult

        if self.approved:
            return ApprovalResult(approved=True, decision=ApprovalDecision.ONCE, reason="ok")
        return ApprovalResult(
            approved=False,
            decision=None,
            reason="Dangerous command: needs approval",
            pattern_key="seg",
        )

    def _apply_decision(self, pattern_key, decision, session_id, command):
        self.applied.append((decision, session_id, command))
        from agent.middlewares.humanInTheLoop.types import ApprovalResult

        return ApprovalResult(
            approved=True, decision=decision, reason="approved", pattern_key=pattern_key
        )


class _FakeMiddleware:
    def __init__(self, config, approval):
        self.config = config
        self.approval = approval

    def _turn_operator(self, _state):
        return "operator"


def _handler_ctx(approval):
    from agent.middlewares.humanInTheLoop.strategies import (
        ApprovalContext,
        ApprovalOutcome,
    )
    from agent.middlewares.humanInTheLoop.types import ApprovalMode, HITLConfig

    return ApprovalContext(
        mw=_FakeMiddleware(HITLConfig(mode=ApprovalMode.MANUAL), approval),
        state={},
        runtime=None,
        session_id="cmd-cache-strategy",
        outcome=ApprovalOutcome(),
    )


def test_terminal_card_offers_session_only_for_cacheable_commands(monkeypatch):
    from agent.middlewares.humanInTheLoop import strategies

    captures: list[list[str]] = []

    def _interrupt(request):
        captures.append(list(request["review_configs"][0]["allowed_decisions"]))
        return {"decisions": [{"type": "session"}]}

    monkeypatch.setattr(strategies, "interrupt", _interrupt)
    approval = _FakeApproval(approved=False)
    ctx = _handler_ctx(approval)
    handler = strategies.TerminalApprovalHandler()

    handled = handler.handle(
        {"name": "terminal", "args": {"command": "npm install lodash"}, "id": "c1"}, ctx
    )

    assert handled is True
    assert captures == [["approve", "session", "reject"]]
    assert approval.applied == [
        (ApprovalDecision.SESSION, "cmd-cache-strategy", "npm install lodash")
    ]
    assert [c["id"] for c in ctx.outcome.revised_tool_calls] == ["c1"]


def test_terminal_card_does_not_offer_session_for_a_dangerous_command(monkeypatch):
    from agent.middlewares.humanInTheLoop import strategies

    captures: list[list[str]] = []

    def _interrupt(request):
        captures.append(list(request["review_configs"][0]["allowed_decisions"]))
        return {"decisions": [{"type": "approve"}]}

    monkeypatch.setattr(strategies, "interrupt", _interrupt)
    approval = _FakeApproval(approved=False)
    ctx = _handler_ctx(approval)
    handler = strategies.TerminalApprovalHandler()

    handler.handle({"name": "terminal", "args": {"command": "rm -rf build/"}, "id": "c2"}, ctx)

    assert captures == [["approve", "reject"]]
    assert approval.applied == []
