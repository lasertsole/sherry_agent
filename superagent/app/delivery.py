"""Delivery requirements and the finish gate (prototype ``src/app/delivery.ts``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from superagent.core.model import ReportKind, Ticket

FAIL_CLOSED_KINDS = frozenset({ReportKind.BLOCKED, ReportKind.FAILED, ReportKind.ATTENTION})


@dataclass(frozen=True)
class DeliveryRequirements:
    """Conditions a Ticket must satisfy before it can be finished."""

    require_ready: bool = True
    require_approval: bool = False
    max_changes: int = 200

    def gate(self, ticket: Ticket, report_kind: ReportKind | None) -> tuple[bool, str]:
        """Return whether a ready report may end the Ticket."""
        if report_kind in FAIL_CLOSED_KINDS:
            return False, "hitlFailClosed"
        if self.require_ready and report_kind != ReportKind.READY:
            return False, "notReady"
        if self.require_approval and not _approved(ticket):
            return False, "approvalMissing"
        changes = _change_count(ticket)
        if self.max_changes and changes > self.max_changes:
            return False, "tooLarge"
        return True, ""


class AgentDelivery:
    """Apply delivery requirements to worker reports."""

    def __init__(self, requirements: DeliveryRequirements | None = None) -> None:
        self.requirements = requirements or DeliveryRequirements()

    def may_finish(self, ticket: Ticket) -> tuple[bool, str]:
        """Check the last report against the finish gate."""
        report = ticket.latest_report
        return self.requirements.gate(ticket, report.kind if report else None)

    @staticmethod
    def from_settings(settings: Any) -> AgentDelivery:
        """Read delivery requirements from settings."""

        def get(name: str, default: Any) -> Any:
            if isinstance(settings, dict):
                return settings.get(name, default)
            return getattr(settings, name, default)

        return AgentDelivery(
            DeliveryRequirements(
                require_ready=bool(get("require_ready", True)),
                require_approval=bool(get("require_approval", False)),
                max_changes=int(get("max_changes", 200)),
            )
        )


def _approved(ticket: Ticket) -> bool:
    report = ticket.latest_report
    if report is None or not isinstance(report.pull_request, dict):
        return False
    return bool(report.pull_request.get("approved", False))


def _change_count(ticket: Ticket) -> int:
    report = ticket.latest_report
    if report is None or not isinstance(report.pull_request, dict):
        return 0
    additions = int(report.pull_request.get("additions", 0))
    deletions = int(report.pull_request.get("deletions", 0))
    return additions + deletions


__all__ = ["AgentDelivery", "DeliveryRequirements", "FAIL_CLOSED_KINDS"]
