"""Session rewind: cut the conversation back to a message, safely.

A rewind hides the messages after ``cut_after_message_id`` (the rows stay in
SQLite; reads filter through ``runtime.session.conversation_branch``) and bumps
the branch generation, which is what fences asynchronous work created before
the cut from landing after it.

The hard constraint from the plan is enforced here rather than left to the
client: a rewind is refused while the session has a turn in flight — the
fencing counter cannot stop a tool call that is already running, so the turn
boundary is the only place the cut is safe. The same rule greys the button out,
because ``GET`` reports it.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from runtime.session.conversation_branch import (
    apply_rewind,
    read_branch,
)

__all__ = ["apply_session_rewind", "rewind_state"]


async def rewind_state(session_id: str) -> dict:
    """What a rewind would do right now, and whether it is allowed."""
    from server.service.session_settings_service import session_turn_active

    branch = read_branch(session_id)
    busy = await session_turn_active(session_id)
    return {
        "session_id": session_id,
        "branch_generation": branch.branch_generation,
        "rewound_at": branch.rewound_at,
        "hidden_ranges": [list(pair) for pair in branch.hidden_ranges],
        "can_rewind": not busy,
    }


async def apply_session_rewind(session_id: str, cut_after_message_id: int) -> dict:
    """Cut the session's conversation back to *cut_after_message_id*.

    :raises ValueError: The session is busy (a turn is in flight or queue rows
        are pending), or the cut does not name a message of this session.
    """
    from server.service.session_settings_service import session_turn_active
    from server.service.turn_runner import set_hitl_pending

    if await session_turn_active(session_id):
        raise ValueError("a turn is running; rewind at the turn boundary")

    from context_engine.store.core import get_max_message_id, message_exists

    if cut_after_message_id > 0:
        exists = await asyncio.to_thread(message_exists, session_id, cut_after_message_id)
        if not exists:
            raise ValueError("cut_after_message_id is not a message of this session")
    tip = await asyncio.to_thread(get_max_message_id, session_id)
    if tip <= cut_after_message_id:
        raise ValueError("nothing to rewind: the cut is already at the tip")

    branch = apply_rewind(
        session_id,
        cut_after_message_id=cut_after_message_id,
        tip_message_id=tip,
    )

    # A pending approval belongs to the branch that was just abandoned: clear
    # the flag so the stale approval cannot resume it (the resume path refuses
    # when nothing is pending).
    try:
        set_hitl_pending(session_id, False)
    except Exception as exc:  # noqa: BLE001 — clearing is best-effort
        logger.warning("rewind: could not clear the HITL flag for {}: {}", session_id, exc)

    logger.info(
        "Session rewound: session={} cut_after={} tip={} generation={}",
        session_id,
        cut_after_message_id,
        tip,
        branch.branch_generation,
    )
    return {
        "session_id": session_id,
        "cut_after_message_id": cut_after_message_id,
        "tip_message_id": tip,
        "branch_generation": branch.branch_generation,
        "hidden_ranges": [list(pair) for pair in branch.hidden_ranges],
    }
