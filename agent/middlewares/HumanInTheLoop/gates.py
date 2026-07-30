"""Gate modules — supplementary HITL gates beyond the core approval pipeline.

Provides:
- :class:`WriteApprovalGate` — Layer 8:  stage memory/skill writes for human approval
- :class:`InterruptManager` — Layer 9:  per-session tool-execution interrupt signalling
- :class:`MCPElicitationConsent` — Layer 11: MCP server user-input consent (fail-closed)
- :class:`KanbanTriage` — Layer 12: task failure escalation to human decision-makers
- :class:`PairingStore` — Layer 13: platform-level user authorization
- :class:`SlashConfirm` — Layer 14: destructive slash-command confirmation
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from .types import (
    ApprovalDecision, ApprovalResult, HITLConfig, WriteTarget,
    TriageStatus, BLOCKED_MESSAGE,
)


@dataclass
class PendingWrite:
    """Represents a single write waiting for human approval.

    Attributes:
        write_id:     UUID string identifying this write request.
        target:      Whether the write targets memory or skills.
        content:     The raw content submitted for approval.
        created_at:  Unix timestamp when this write was staged.
        approved:    ``True``  if approved, ``False``  if rejected, ``None``  if pending.
    """
    write_id: str
    target: WriteTarget
    content: str
    created_at: float = field(default_factory=time.time)
    approved: bool | None = None


class WriteApprovalGate:
    """Layer 8: memory/skills write staging and approval."""

    def __init__(self, config: HITLConfig):
        self.config = config
        self._pending_writes: dict[str, list[PendingWrite]] = {}

    def request_write(self, target: WriteTarget, content: str, session_id: str) -> ApprovalResult:
        """Stage a write for human approval, or auto-approve if the gate is disabled.

        Returns:
            ``ApprovalResult(approved=True)``  when the gate is off for *target*,
            otherwise ``ApprovalResult(approved=False, decision=DENY)``  with a staged write ID.
        """
        gate_on = (
            self.config.write_approval_memory if target == WriteTarget.MEMORY
            else self.config.write_approval_skills
        )
        if not gate_on:
            return ApprovalResult(approved=True, decision=ApprovalDecision.ONCE, reason="Write approval gate off")

        write_id = str(uuid.uuid4())
        pending = PendingWrite(write_id=write_id, target=target, content=content)
        self._pending_writes.setdefault(session_id, []).append(pending)
        return ApprovalResult(
            approved=False, decision=ApprovalDecision.DENY,
            reason=f"Write staged for approval (id={write_id}). Use approve_write/reject_write.",
        )

    def approve_write(self, session_id: str, write_id: str) -> bool:
        """Approve a previously staged write by its ID.

        Returns:
            ``True``  if the write was found and marked approved.
        """
        for w in self._pending_writes.get(session_id, []):
            if w.write_id == write_id:
                w.approved = True
                return True
        return False

    def reject_write(self, session_id: str, write_id: str) -> bool:
        """Reject and remove a previously staged write by its ID.

        Returns:
            ``True``  if the write was found and removed.
        """
        writes = self._pending_writes.get(session_id, [])
        for i, w in enumerate(writes):
            if w.write_id == write_id:
                writes.pop(i)
                return True
        return False

    def get_pending_writes(self, session_id: str, target: WriteTarget | None = None) -> list[PendingWrite]:
        """Retrieve all pending writes for a session, optionally filtered by target.

        Returns:
            A (possibly empty) list of :class:`PendingWrite` instances.
        """
        writes = self._pending_writes.get(session_id, [])
        return [w for w in writes if w.target == target] if target else writes


class InterruptManager:
    """Layer 9: per-session interrupt signalling.

    Provides ``threading.Event``-based interrupt flags that,
    when set, cause active tool-execution hooks to short-circuit
    and return a blocked ``ToolMessage``.
    """

    def __init__(self):
        self._flags: dict[str, threading.Event] = {}

    def set_interrupt(self, session_id: str, active: bool = True):
        """Set or clear the interrupt flag for a given session.

        Args:
            session_id: Target session identifier.
            active: ``True``  to set the interrupt, ``False``  to clear it.
        """
        if active:
            event = threading.Event()
            event.set()
            self._flags[session_id] = event
        else:
            self._flags.pop(session_id, None)

    def is_interrupted(self, session_id: str) -> bool:
        """Check whether the interrupt flag is currently set for a session.

        Returns:
            ``True``  if an interrupt is active.
        """
        return session_id in self._flags and self._flags[session_id].is_set()

    def clear_interrupt(self, session_id: str):
        """Clear and remove the interrupt flag for a session."""
        self._flags.pop(session_id, None)


class MCPElicitationConsent:
    """Layer 11: MCP server user-input consent. Fail-closed.

    Currently always denies — the system defaults to blocking
    any MCP server-initiated user-input elicitation unless the
    user explicitly approves out-of-band.
    """

    @staticmethod
    def request_consent(server_name: str, session_id: str) -> ApprovalResult:
        """Request user consent for an MCP server to elicit user input.

        Returns:
            Always an ``approved=False``, ``decision=DENY`` result.
        """
        return ApprovalResult(
            approved=False, decision=ApprovalDecision.DENY,
            reason=f"MCP elicitation from '{server_name}' requires consent. Defaulting to deny.",
        )


class KanbanTriage:
    """Layer 12: task failure escalation to human decision-makers.

    Counts consecutive task failures per session. When a task
    exceeds ``recurrence_limit`` failures it is escalated to
    ``TriageStatus.TRIAGE`` instead of ``TriageStatus.BLOCKED``.
    """

    def __init__(self, recurrence_limit: int):
        self._limit = recurrence_limit
        self._failures: dict[str, dict[str, int]] = {}

    def report_task_failure(self, task_id: str, session_id: str) -> TriageStatus:
        """Record a task failure and return the escalation status.

        Returns:
            ``TriageStatus.TRIAGE``  if the recurrence limit is reached,
            ``TriageStatus.BLOCKED`` otherwise.
        """
        self._failures.setdefault(session_id, {})
        failures = self._failures[session_id]
        failures[task_id] = failures.get(task_id, 0) + 1
        return TriageStatus.TRIAGE if failures[task_id] >= self._limit else TriageStatus.BLOCKED

    def resolve_triage(self, task_id: str, session_id: str):
        """Clear the failure counter for a previously escalated task."""
        if session_id in self._failures:
            self._failures[session_id].pop(task_id, None)


class PairingStore:
    """Layer 13: platform-level user authorization.

    Tracks which platform/user pairs are allowed to interact
    with the agent. Defaults to deny for unknown users.
    """

    def __init__(self):
        self._store: dict[str, dict[str, bool]] = {}

    def is_user_allowed(self, platform: str, user_id: str) -> bool:
        """Check whether a user is authorised on a given platform.

        Returns:
            ``True``  if the user has been explicitly approved.
        """
        return self._store.get(platform, {}).get(user_id, False)

    def approve_user(self, platform: str, user_id: str):
        """Grant access to a user on a specific platform."""
        self._store.setdefault(platform, {})[user_id] = True

    def revoke_user(self, platform: str, user_id: str):
        """Revoke access for a user on a specific platform."""
        if platform in self._store:
            self._store[platform].pop(user_id, None)


class SlashConfirm:
    """Layer 14: destructive slash command confirmation.

    Intercepts destructive actions (e.g. file deletion, shutdown)
    and requires explicit human confirmation before proceeding.
    """

    def __init__(self, config: HITLConfig):
        self.config = config

    def confirm_destructive(self, action: str, session_id: str) -> ApprovalResult:
        """Request confirmation for a destructive action.

        Returns:
            ``ApprovalResult(approved=True)``  when the gate is off,
            otherwise ``ApprovalResult(approved=False, decision=DENY)``.
        """
        if not self.config.destructive_slash_confirm:
            return ApprovalResult(approved=True, reason="Destructive confirmation disabled")
        return ApprovalResult(
            approved=False, decision=ApprovalDecision.DENY,
            reason=f"Destructive action '{action}' requires confirmation. Defaulting to deny.",
        )
