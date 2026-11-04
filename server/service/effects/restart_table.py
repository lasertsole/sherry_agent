"""The restart terminal-declaration table.

One place that names every state machine whose non-terminal rows must be forced
to a terminal state after a process restart, plus the function that does it.
The table is intentionally thin: it does NOT re-implement the recovery logic.
``apply_subagent_terminals`` and ``apply_queue_terminals`` call the existing,
already-tested recovery helpers, both of which are idempotent.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from loguru import logger

__all__ = [
    "RestartTerminal",
    "all_terminals",
    "apply_all_terminals",
    "apply_queue_terminals",
    "apply_subagent_terminals",
    "register_restart_terminal",
]


@dataclass(frozen=True)
class RestartTerminal:
    """One state machine's restart rule (metadata + an idempotent apply call)."""

    name: str
    apply_fn: Callable[[], Awaitable[int | None]]
    non_terminals: frozenset[str]
    force_to: str
    extra: dict[str, str] = field(default_factory=dict)


async def apply_subagent_terminals() -> int | None:
    """Force restored PENDING/RUNNING subagent runs to their terminal state.

    ``restore_runs_from_disk`` already does the full restart recovery: PENDING →
    ``TIMEOUT/pending_orphaned``, RUNNING → ``TIMEOUT/process_restarted``,
    monotonic rebase and PENDING delivery. Its process-level ``_restore_done``
    guard makes the second call a no-op, so this table and the normal boot
    reconcile can both call it.
    """
    from agent.tools.subagent.registry.state import restore_runs_from_disk

    await restore_runs_from_disk()
    return None


async def apply_queue_terminals() -> int | None:
    """Return CLAIMED user-input rows to QUEUED; returns the requeued count.

    Delegates to the boot-recovery helper, which iterates the candidate
    sessions; the SQL only touches ``CLAIMED`` rows, so a second call is a
    no-op.
    """
    from server.service.boot_recovery import requeue_claimed_inputs

    return await requeue_claimed_inputs()


_RESTART_TERMINALS: list[RestartTerminal] = [
    RestartTerminal(
        name="subagent_runs",
        apply_fn=apply_subagent_terminals,
        non_terminals=frozenset({"PENDING", "RUNNING", "INTERRUPTED"}),
        force_to="TIMEOUT",
        extra={"pause_reason": "process_restart", "ended_reason": "process_restarted"},
    ),
    RestartTerminal(
        name="user_input_queue",
        apply_fn=apply_queue_terminals,
        non_terminals=frozenset({"CLAIMED"}),
        force_to="QUEUED",
    ),
]


def register_restart_terminal(spec: RestartTerminal) -> None:
    """Add a state machine to the table (used by future stores)."""
    _RESTART_TERMINALS.append(spec)


def all_terminals() -> list[RestartTerminal]:
    return list(_RESTART_TERMINALS)


async def apply_all_terminals() -> dict[str, int | None]:
    """Run every declared apply function, fail-open per entry."""
    applied: dict[str, int | None] = {}
    for spec in all_terminals():
        try:
            applied[spec.name] = await spec.apply_fn()
        except Exception:  # noqa: BLE001 - one broken state machine never blocks boot
            logger.exception("effects: restart terminal apply failed for {}", spec.name)
    return applied
