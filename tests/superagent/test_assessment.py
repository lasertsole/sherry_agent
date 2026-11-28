"""MasterAssessor and delivery gate tests."""

from __future__ import annotations

from superagent.app.assessment import MasterAssessor, assessment_outcome
from superagent.app.delivery import AgentDelivery, DeliveryRequirements
from superagent.app.sessions import hitl_report
from superagent.core.memory import MemoryRepositories
from superagent.core.model import DecisionOutcome, Eval, Report, ReportKind, SourceRef, Thread
from superagent.policy import settings_policy
from tests.superagent.conftest import FakeEffects, make_ticket, make_trigger, policy_settings


def build():
    repo = MemoryRepositories()
    trigger = make_trigger()
    repo.triggers.save(trigger)
    thread = Thread(
        key="tk",
        source="github",
        id="thread-1",
        eval=Eval(
            id="eval-1",
            thread=SourceRef("github", "thread-1"),
            primary=trigger.id,
            trigger_ids=[trigger.id],
        ),
    )
    repo.threads.save(thread)
    repo.tickets.save(make_ticket())

    def decide(*_args, **_kwargs) -> None:
        return None

    engine = type("Engine", (), {"repo": repo, "decide": decide})()
    host = type(
        "Host",
        (),
        {"engine": engine, "policy": lambda self: settings_policy(policy_settings(), "github")},
    )()
    effects = FakeEffects()
    return MasterAssessor(host, effects), engine, effects, trigger


def test_request_caches_context_and_releases() -> None:
    assessor, _engine, effects, trigger = build()
    assessor.request("eval-1", [trigger.id])
    assert effects.items[0][0] == "assessment"
    card = assessor.context("eval-1")
    assert card.trigger_text == trigger.text
    assert card.policy_version == settings_policy(policy_settings(), "github").version
    assert assessor.context("eval-1") is card
    assessor.release("eval-1")
    assert assessor.context("eval-1") is not card


def test_outcome_auto_observe_off_and_failure() -> None:
    verdict = {"matched_rules": ["rule-1"]}
    assert (
        assessment_outcome(verdict, accept_mode="auto", strength="inferred")[0]
        == DecisionOutcome.ACCEPT
    )
    assert (
        assessment_outcome(verdict, accept_mode="observe", strength="inferred")[0]
        == DecisionOutcome.IGNORE
    )
    assert (
        assessment_outcome(verdict, accept_mode="off", strength="inferred")[1].code == "acceptOff"
    )
    assert (
        assessment_outcome({"matched_rules": []}, accept_mode="auto", strength="inferred")[1].code
        == "ignoredFromCard"
    )
    assert (
        assessment_outcome({"error": "boom"}, accept_mode="auto", strength="explicit")[0]
        == DecisionOutcome.UNAVAILABLE
    )


def test_finish_gate_requires_ready_and_blocks_hitl() -> None:
    ticket = make_ticket()
    delivery = AgentDelivery(DeliveryRequirements())
    assert delivery.may_finish(ticket) == (False, "notReady")
    ticket.latest_report = Report(
        assignment_id="assign-1", kind=ReportKind.READY, summary="done", at="t"
    )
    assert delivery.may_finish(ticket) == (True, "")
    ticket.latest_report = hitl_report("need approval", "t", "assign-1")
    assert delivery.may_finish(ticket) == (False, "hitlFailClosed")
    assert ticket.latest_report.kind == ReportKind.BLOCKED
    assert ticket.latest_report.question == "need approval"
