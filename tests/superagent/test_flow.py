"""Ticket↔TaskFlow mapping and long-unattended wake tests."""

from __future__ import annotations

from superagent.app.delivery import AgentDelivery, DeliveryRequirements
from superagent.app.flow import FlowMap, wake_idle_ticket, waiting_after_cap
from superagent.core.model import AssignmentStatus, Report, ReportKind
from superagent.core.stage import TicketStage, ticket_stage
from tests.superagent.conftest import make_ticket, make_trigger


def test_flow_map_is_one_to_one_and_has_ready_gate() -> None:
    flow = FlowMap(AgentDelivery(DeliveryRequirements()))
    ticket = make_ticket()
    assert flow.finish_allowed(ticket) == (False, "flowMissing")
    first = flow.bind(ticket.id, "flow-1", "session-1")
    assert flow.by_ticket(ticket.id) == first
    assert flow.by_flow("flow-1") == first
    flow.bind(ticket.id, "flow-2")
    assert flow.by_flow("flow-1") is None
    assert flow.finish_allowed(ticket) == (False, "notReady")
    ticket.latest_report = Report(
        assignment_id="assign-1", kind=ReportKind.READY, summary="done", at="t"
    )
    assert flow.finish_allowed(ticket) == (True, "")
    assert flow.unbind(ticket.id) is True
    assert flow.unbind(ticket.id) is False


def test_waiting_cap_leaves_ticket_idle_and_trigger_wakes_new_assignment() -> None:
    ticket = make_ticket()
    ticket.assignments[0].status = AssignmentStatus.ENDED
    ticket.latest_report = waiting_after_cap(ticket.id, "assign-1", "t1")
    assert ticket.latest_report.kind == ReportKind.WAITING
    assert ticket_stage(ticket) == TicketStage.IDLE

    class Tickets:
        def save(self, value) -> None:
            return None

    class Effects:
        def __init__(self) -> None:
            self.items: list[tuple] = []

        def enqueue(self, *args) -> None:
            self.items.append(args)

    class Atomic:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    class Engine:
        def __init__(self) -> None:
            self.repo = type("Repo", (), {"tickets": Tickets(), "atomic": Atomic()})()
            self.effects = Effects()

    engine = Engine()
    trigger = make_trigger("trig-wake")
    assert wake_idle_ticket(engine, ticket, trigger) is True
    assert len(ticket.assignments) == 2
    assert ticket.assignments[-1].status == AssignmentStatus.PENDING
    assert ticket.inputs[-1].kind == "wake"
    assert engine.effects.items[0][0] == "launch"
    assert wake_idle_ticket(engine, ticket, trigger) is False  # no longer idle
