"""Session project-directory binding (client UI → ``/sessions/project``).

The fourth per-session setting, shaped after the thinking / main-model pair: the
value lives in the session state register (``StateKey.PROJECT_DIR``), so it
survives restarts through the durable mirror and is visible to every layer
without new plumbing. ``PROJECT_DIR_PENDING`` is the parked twin: a choice made
while a turn is running lands on the next turn boundary (the promotion itself
lives in ``promote_pending_project_dir`` — same code path as the other two
settings, see ``turn_runner.on_turn_finished``).

Precedence when reading: the session's binding, else the process default
(``SHERRY_PROJECT_DIR`` → ``sherry.jsonc`` ``project_dir`` → ``ROOT_DIR``). The
``source`` field is always reported so a silent fallback is visible in the API
and can be surfaced in the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from config.path import validate_project_dir
from pub.func.validator import is_safe_session_id
from runtime.session.project_dir import (
    current_project_dir,
    project_dir_source,
    read_project_dir_durable,
    write_pending_project_dir,
    write_project_dir,
)
from runtime.session.state_keys import StateKey

__all__ = [
    "ProjectDirState",
    "apply_project_choice",
    "get_project_state",
    "promote_pending_project_dir",
]


@dataclass(frozen=True)
class ProjectDirState:
    """One session's project-directory view.

    ``directory`` is the session's own binding (``None`` = unbound, the UI's
    "未绑定" state); ``effective`` is where the tools actually resolve — the
    binding when present, otherwise the process default. ``pending`` carries the
    parked choice while a turn is in flight.
    """

    directory: str | None
    effective: str
    source: str
    pending: str | None


def _state_for(session_id: str, pending: str | None) -> ProjectDirState:
    bound = read_project_dir_durable(session_id)
    return ProjectDirState(
        directory=str(bound) if bound is not None else None,
        effective=str(current_project_dir(session_id)),
        source=project_dir_source(session_id),
        pending=pending,
    )


def _read_pending(session_id: str) -> str | None:
    from runtime.session import state_register_db
    from runtime.session.state_register import state_register_mem

    raw: Any = state_register_mem.get_state(session_id, StateKey.PROJECT_DIR_PENDING, None)
    if raw is None:
        raw = state_register_db.get_state(session_id, StateKey.PROJECT_DIR_PENDING, None)
    return str(raw) if isinstance(raw, str) and raw.strip() else None


def get_project_state(session_id: str) -> ProjectDirState:
    """Read the session's project-directory state (blocking: wrap in a thread)."""
    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")
    state = _state_for(session_id, _read_pending(session_id))
    if state.source != "session":
        logger.debug(
            "Project dir: session {} is unbound; resolving against {} ({})",
            session_id,
            state.effective,
            state.source,
        )
    return state


def apply_project_choice(session_id: str, requested: str | None) -> ProjectDirState:
    """Bind (or clear, with ``None``) the session's project directory.

    ``requested`` must be an absolute, existing directory —
    :func:`config.path.validate_project_dir` normalises and validates it, and
    its message is safe to surface to the client. A rejected value never
    touches the stored state.

    Synchronous direct entry point (tests / idle callers). The HTTP layer goes
    through :func:`apply_project_choice_async`, which parks the value while a
    turn is in flight so the running turn finishes against the root it started
    with (the same contract as the thinking / main-model controls).
    """
    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")

    if requested is None:
        # Clearing unpins the session from any explicit binding: remove the key
        # so the process default applies again (an empty string would be a
        # "bound to nowhere" value every reader would have to special-case).
        from runtime import state_register_db
        from runtime.session.state_register import state_register_mem

        state_register_mem.delete_state(session_id, StateKey.PROJECT_DIR)
        state_register_db.delete_state(session_id, StateKey.PROJECT_DIR)
        logger.info("Project dir: session {} binding cleared (process default applies)", session_id)
        return _state_for(session_id, _read_pending(session_id))

    directory = validate_project_dir(requested)
    write_project_dir(session_id, directory, pending=False)
    logger.info("Project dir: session {} bound to {}", session_id, directory)
    return _state_for(session_id, _read_pending(session_id))


async def apply_project_choice_async(session_id: str, requested: str | None) -> ProjectDirState:
    """HTTP entry for ``PUT /sessions/project``; parks while a turn is running.

    The busy decision reuses the input queue's per-session lock and the same
    four signals every other control uses, so check-and-write stays atomic
    against a racing submit. A parked choice lands at the next turn boundary
    (``turn_runner.on_turn_finished`` promotes it) — never mid-turn, because the
    LLM's transcript carries no record of a directory change and a half-switched
    turn could write to two roots.
    """
    import asyncio

    from loguru import logger as _logger

    from agent.tools.subagent.registry.session_state import normalize_session_key
    from server.queue.user_input_queue import UserInputQueueStatus
    from server.service.input_queue_service import _get_session_lock, get_default_queue

    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")

    # Validate BEFORE the lock so a rejected value never mutates anything.
    target: str | None = None if requested is None else str(validate_project_dir(requested))

    from server.service.session_settings_service import _session_turn_active

    async with _get_session_lock(session_id):
        busy = _session_turn_active(session_id)
        if not busy:
            try:
                queue = get_default_queue()
                rows = await queue.list_active(normalize_session_key(session_id))
                busy = any(
                    row.status in (UserInputQueueStatus.QUEUED, UserInputQueueStatus.CLAIMED)
                    for row in rows
                )
            except Exception:  # pragma: no cover - defensive, queue may be absent
                busy = False

        if not busy:
            state = await asyncio.to_thread(apply_project_choice, session_id, target)
            if state.pending is not None:
                # A live write supersedes any parked choice.
                await asyncio.to_thread(park_project_choice, session_id, None)
                state = await asyncio.to_thread(_state_for, session_id, None)
            # No announcement here: the agent is told at SEND time, by
            # WorkspaceNoticeMiddleware, which compares the root against the one
            # the agent was last told about and splices ONE notice in front of
            # the next user message (any number of switches coalesce into it).
            return state

        state = await asyncio.to_thread(park_project_choice, session_id, target)
        _logger.info(
            "Project dir parked: session={} target={} (a turn is in flight)",
            session_id,
            target,
        )
        return state


def promote_pending_project_dir(session_id: str) -> str | None:
    """Promote a parked directory choice to the live key; ``True`` when it moved.

    Called at the turn boundary (``turn_runner.on_turn_finished``) for idle
    sessions only; while a HITL decision is pending the caller skips promotion so
    the suspended graph resumes against the directory it started with.
    """
    parked = _read_pending(session_id)
    if parked is None:
        return None
    write_project_dir(session_id, parked, pending=False)
    write_pending_project_dir(session_id, None)
    logger.info("Project dir: promoted parked choice for session {} -> {}", session_id, parked)
    return parked


def park_project_choice(session_id: str, requested: str | None) -> ProjectDirState:
    """Park (or clear the parked) choice for the next turn boundary."""
    if not session_id or not is_safe_session_id(session_id):
        raise ValueError("invalid session_id")
    if requested is None:
        write_pending_project_dir(session_id, None)
        return _state_for(session_id, None)
    directory = validate_project_dir(requested)
    write_pending_project_dir(session_id, directory)
    logger.info("Project dir: parked choice for session {} -> {}", session_id, directory)
    return _state_for(session_id, str(directory))
