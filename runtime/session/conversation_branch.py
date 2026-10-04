"""Conversation rewind: which of a session's messages are still on the branch.

A rewind does not delete anything (the store is append-only by discipline —
``context_engine`` messages and the snapshot rows are never rewritten in
place). It records which id RANGE the rewind removed from view, and every read
path filters through :func:`filter_visible`: the messages stay in SQLite, the
prompt and the chat simply stop showing them, and a message sent AFTER the
rewind (a larger id) is visible again immediately.

The same record carries ``branch_generation`` and ``rewound_at``: the fencing
that keeps asynchronous work created before the rewind from landing after it
(a subagent's completion announcement, a memory nudge). Generation is a
monotonic counter — comparing timestamps alone would be at the mercy of clock
granularity — while ``rewound_at`` is what the announce path compares against
the run's creation time.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

from .state_keys import StateKey

__all__ = [
    "ConversationBranch",
    "apply_rewind",
    "branch_generation",
    "clear_branch",
    "filter_visible",
    "is_visible",
    "read_branch",
    "rewound_at",
]


@dataclass(frozen=True, slots=True)
class ConversationBranch:
    """The active-branch record of one session.

    ``hidden_ranges`` holds half-open ``(after_id, up_to_id]`` pairs: a message
    is hidden when its id falls in one of them. A later rewind unions another
    range in, so rewinding twice keeps both cuts hidden.
    """

    hidden_ranges: tuple[tuple[int, int], ...] = ()
    branch_generation: int = 0
    rewound_at: float = 0.0

    def as_state(self) -> dict[str, Any]:
        return {
            "hidden_ranges": [list(pair) for pair in self.hidden_ranges],
            "branch_generation": self.branch_generation,
            "rewound_at": self.rewound_at,
        }

    @classmethod
    def from_state(cls, raw: Any) -> ConversationBranch:
        if not isinstance(raw, dict):
            return cls()
        ranges: list[tuple[int, int]] = []
        for pair in raw.get("hidden_ranges") or []:
            try:
                low, high = int(pair[0]), int(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
            if high > low:
                ranges.append((low, high))
        return cls(
            hidden_ranges=tuple(sorted(ranges)),
            branch_generation=int(raw.get("branch_generation") or 0),
            rewound_at=float(raw.get("rewound_at") or 0.0),
        )


def read_branch(session_id: str) -> ConversationBranch:
    """The session's branch record (an empty one when it was never rewound)."""
    if not session_id:
        return ConversationBranch()
    try:
        from runtime import state_register_db
        from .state_register import state_register_mem

        raw = state_register_mem.get_state(session_id, StateKey.CONVERSATION_BRANCH, None)
        if raw is None:
            raw = state_register_db.get_state(session_id, StateKey.CONVERSATION_BRANCH, None)
        if isinstance(raw, str) and raw:
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return ConversationBranch()
        return ConversationBranch.from_state(raw)
    except Exception as exc:  # noqa: BLE001 — a read must never break a caller
        logger.warning("conversation branch read failed for {}: {}", session_id, exc)
        return ConversationBranch()


def _write_branch(session_id: str, branch: ConversationBranch) -> None:
    from runtime import state_register_db
    from .state_register import state_register_mem

    value = json.dumps(branch.as_state(), ensure_ascii=False)
    state_register_mem.set_state(session_id, StateKey.CONVERSATION_BRANCH, value)
    state_register_db.set_state(session_id, StateKey.CONVERSATION_BRANCH, value)


def branch_generation(session_id: str) -> int:
    """The session's fencing counter (0 before the first rewind)."""
    return read_branch(session_id).branch_generation


def rewound_at(session_id: str) -> float:
    """When the session was last rewound (0.0 when never)."""
    return read_branch(session_id).rewound_at


def is_visible(branch: ConversationBranch, message_id: int) -> bool:
    """Whether a message id is on the active branch."""
    for low, high in branch.hidden_ranges:
        if low < message_id <= high:
            return False
    return True


def filter_visible(session_id: str, messages: list[dict]) -> list[dict]:
    """Drop the messages a rewind removed from view (no-op without one)."""
    branch = read_branch(session_id)
    if not branch.hidden_ranges:
        return messages
    kept: list[dict] = []
    for message in messages:
        message_id = message.get("id")
        if isinstance(message_id, int) and not is_visible(branch, message_id):
            continue
        kept.append(message)
    return kept


def apply_rewind(
    session_id: str,
    *,
    cut_after_message_id: int,
    tip_message_id: int,
) -> ConversationBranch:
    """Hide everything after *cut_after_message_id*, up to the current tip.

    :param cut_after_message_id: The last message the active branch keeps.
    :param tip_message_id: The newest message id at rewind time — the upper end
        of the hidden range. Messages created later have larger ids and are
        therefore visible again.
    """
    branch = read_branch(session_id)
    ranges = list(branch.hidden_ranges)
    if tip_message_id > cut_after_message_id:
        ranges.append((cut_after_message_id, tip_message_id))
    merged: list[tuple[int, int]] = []
    for low, high in sorted(ranges):
        if merged and low <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    updated = ConversationBranch(
        hidden_ranges=tuple(merged),
        branch_generation=branch.branch_generation + 1,
        rewound_at=time.time(),
    )
    _write_branch(session_id, updated)
    logger.info(
        "Conversation rewound: session={} cut_after={} tip={} generation={}",
        session_id,
        cut_after_message_id,
        tip_message_id,
        updated.branch_generation,
    )
    return updated


def clear_branch(session_id: str) -> None:
    """Forget the branch record (session deletion, tests)."""
    from runtime import state_register_db
    from .state_register import state_register_mem

    state_register_mem.delete_state(session_id, StateKey.CONVERSATION_BRANCH)
    state_register_db.delete_state(session_id, StateKey.CONVERSATION_BRANCH)
