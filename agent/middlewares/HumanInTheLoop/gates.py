"""Gate modules: write approval, interrupt, MCP consent, kanban triage, pairing, slash confirm."""

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
        for w in self._pending_writes.get(session_id, []):
            if w.write_id == write_id:
                w.approved = True
                return True
        return False

    def reject_write(self, session_id: str, write_id: str) -> bool:
        writes = self._pending_writes.get(session_id, [])
        for i, w in enumerate(writes):
            if w.write_id == write_id:
                writes.pop(i)
                return True
        return False

    def get_pending_writes(self, session_id: str, target: WriteTarget | None = None) -> list[PendingWrite]:
        writes = self._pending_writes.get(session_id, [])
        return [w for w in writes if w.target == target] if target else writes


class InterruptManager:
    """Layer 9: per-session interrupt signalling."""

    def __init__(self):
        self._flags: dict[str, threading.Event] = {}

    def set_interrupt(self, session_id: str, active: bool = True):
        if active:
            event = threading.Event()
            event.set()
            self._flags[session_id] = event
        else:
            self._flags.pop(session_id, None)

    def is_interrupted(self, session_id: str) -> bool:
        return session_id in self._flags and self._flags[session_id].is_set()

    def clear_interrupt(self, session_id: str):
        self._flags.pop(session_id, None)


class MCPElicitationConsent:
    """Layer 11: MCP server user-input consent. Fail-closed."""

    @staticmethod
    def request_consent(server_name: str, session_id: str) -> ApprovalResult:
        return ApprovalResult(
            approved=False, decision=ApprovalDecision.DENY,
            reason=f"MCP elicitation from '{server_name}' requires consent. Defaulting to deny.",
        )


class KanbanTriage:
    """Layer 12: task failure escalation to human decision-makers."""

    def __init__(self, recurrence_limit: int):
        self._limit = recurrence_limit
        self._failures: dict[str, dict[str, int]] = {}

    def report_task_failure(self, task_id: str, session_id: str) -> TriageStatus:
        self._failures.setdefault(session_id, {})
        failures = self._failures[session_id]
        failures[task_id] = failures.get(task_id, 0) + 1
        return TriageStatus.TRIAGE if failures[task_id] >= self._limit else TriageStatus.BLOCKED

    def resolve_triage(self, task_id: str, session_id: str):
        if session_id in self._failures:
            self._failures[session_id].pop(task_id, None)


class PairingStore:
    """Layer 13: platform-level user authorization."""

    def __init__(self):
        self._store: dict[str, dict[str, bool]] = {}

    def is_user_allowed(self, platform: str, user_id: str) -> bool:
        return self._store.get(platform, {}).get(user_id, False)

    def approve_user(self, platform: str, user_id: str):
        self._store.setdefault(platform, {})[user_id] = True

    def revoke_user(self, platform: str, user_id: str):
        if platform in self._store:
            self._store[platform].pop(user_id, None)


class SlashConfirm:
    """Layer 14: destructive slash command confirmation."""

    def __init__(self, config: HITLConfig):
        self.config = config

    def confirm_destructive(self, action: str, session_id: str) -> ApprovalResult:
        if not self.config.destructive_slash_confirm:
            return ApprovalResult(approved=True, reason="Destructive confirmation disabled")
        return ApprovalResult(
            approved=False, decision=ApprovalDecision.DENY,
            reason=f"Destructive action '{action}' requires confirmation. Defaulting to deny.",
        )
