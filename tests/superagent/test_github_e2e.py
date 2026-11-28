"""P2 chain: GitHub issue → Ticket → worker → PR → Ticket end (all mocked)."""

from __future__ import annotations

import pytest

from superagent.app.delivery import AgentDelivery, DeliveryRequirements
from superagent.core.memory import MemoryRepositories
from superagent.core.model import (
    AddressKind,
    DecisionOutcome,
    Reason,
    Report,
    ReportKind,
    TicketOutcome,
)
from superagent.executor.sherry import SherryExecutor
from superagent.sources.github_pull import claim_pull
from superagent.sources.github_trigger import GithubConfig, github_trigger
from tests.superagent.conftest import (
    FakeExecutor,
    make_engine,
    policy_settings,
)

CONFIG = GithubConfig(
    repository="acme/widgets",
    cli="gh",
    agent_login="agent-bot",
    observe_issues=True,
    observe_pulls=True,
)


class Client:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def post(self, path: str, payload: dict) -> dict:
        self.calls.append(path)
        return {"run_id": "run-chain"}


class Settings:
    max_workers = 1
    executor_model = "model-x"
    prepare = ""


@pytest.mark.asyncio
async def test_issue_to_pr_single_chain() -> None:
    repo = MemoryRepositories()
    settings = policy_settings(accept_mode="auto")
    engine, ports = make_engine(settings=settings, repo=repo)
    fake_executor: FakeExecutor = ports["executor"]

    event = {
        "id": "issue-1",
        "updated_at": "2026-11-28T00:00:00+00:00",
        "type": "issue",
        "body": "@agent-bot please fix the widget",
        "user": {"login": "alice"},
        "issue": {"number": 42},
        "repository": {"full_name": "acme/widgets"},
        "html_url": "https://github.com/acme/widgets/issues/42",
    }
    trigger = github_trigger(event, CONFIG)
    assert trigger is not None
    trigger = trigger.__class__(
        **{**trigger.__dict__, "addressed": AddressKind.MENTION, "human": True}
    )

    assert engine.receive(trigger) == "stored"
    engine.evaluate_due()
    engine.decide(trigger.id, DecisionOutcome.ACCEPT, Reason("accepted", {"rules": ["rule-1"]}))

    tickets = [ticket for ticket in repo.tickets.list() if ticket.is_open]
    assert len(tickets) == 1
    ticket = tickets[0]
    assert ticket.mandate.matched_rules == ["rule-1"]
    assert fake_executor.started == []  # P1 stand-in; the real Effect drives the launch
    launch = next(item for item in ports["effects"].items if item[0] == "launch")
    assert launch[1] == ticket.id

    # Swap in the real Executor for the launch half of the chain.
    client = Client()
    host = type("Host", (), {"engine": engine, "effects": ports["effects"]})()
    executor = SherryExecutor(host, client, settings_getter=lambda: Settings())
    executor.install()
    await executor._handle_launch(launch[2])
    assert client.calls == ["/internal/superagent/spawn"]

    # Worker reports ready with a PR; finish gate passes before Ticket end.
    engine.report(
        ticket.id,
        Report(
            assignment_id=ticket.assignments[-1].id,
            kind=ReportKind.READY,
            summary="opened PR #7",
            at="2026-11-28T00:05:00+00:00",
            pull_request={"number": 7, "additions": 10, "deletions": 5},
        ),
    )
    stored = repo.tickets.get(ticket.id)
    assert stored is not None
    delivery = AgentDelivery(
        DeliveryRequirements(require_ready=True, require_approval=False, max_changes=200)
    )
    assert delivery.may_finish(stored) == (True, "")

    stored.follows.append(claim_pull(stored, 7, "feature"))
    repo.tickets.save(stored)
    engine.tickets.end(stored, TicketOutcome.MERGED, "2026-11-28T00:10:00+00:00")

    ended = repo.tickets.get(ticket.id)
    assert ended is not None and ended.ended is not None
    assert ended.ended.get("outcome") == TicketOutcome.MERGED.value
