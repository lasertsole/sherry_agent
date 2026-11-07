"""Shadow-checkpoint token of a session's working root — read, remembered, announced.

The workspace notice middleware needs one fact at the start of every turn: the
per-project shadow checkpoint the session's project directory is on RIGHT NOW,
so it can tell the agent when that moved. The token is
``checkpoint@<short-hash>`` from the shadow store; the user's own ``.git`` is
never consulted.

This module is the shared leaf for that fact, mirroring
``runtime.session.project_dir``: the reader is mem-only and cheap, while the
"what the agent was told" baseline lives in the session state register under
``StateKey.GIT_HEAD_ANNOUNCED`` with a durable mirror that the boot prime warms.
"""

from __future__ import annotations

from pathlib import Path

from pub.git_head import shadow_head
from pub.func.message.workspace_notice import (
    GIT_HEAD_NOTICE_ORIGIN,
    workspace_notice_metadata,
)
from runtime.session.state_keys import StateKey

__all__ = [
    "GIT_HEAD_NOTICE_METADATA",
    "GIT_HEAD_NOTICE_ORIGIN",
    "announced_git_head",
    "git_head_notice_text",
    "prime_git_head_from_store",
    "read_git_head",
    "record_announced_git_head",
]

#: Metadata carried by the workspace-checkpoint notice (shared notice shape).
GIT_HEAD_NOTICE_METADATA = workspace_notice_metadata(GIT_HEAD_NOTICE_ORIGIN)

#: Characters of the commit hash kept in the token (a row label, not an id).
_SHORT_HASH_CHARS = 8


def read_git_head(directory: str | Path) -> str | None:
    """The root's shadow checkpoint as ``checkpoint@<short-hash>``; ``None`` if none.

    Reads the per-project shadow store only; the user's own ``.git`` is never
    consulted. A missing store/ref (or git) simply means "nothing to announce".
    """
    try:
        head = shadow_head(Path(directory))
    except (FileNotFoundError, OSError):
        return None
    return f"checkpoint@{head[:_SHORT_HASH_CHARS]}" if head else None


def announced_git_head(session_id: str | None) -> str | None:
    """The shadow-checkpoint token the agent was last TOLD about (mem tier).

    ``None`` = never told (the first turn of a session baselines silently).
    """
    if not session_id:
        return None
    from runtime.session.state_register import state_register_mem

    raw = state_register_mem.get_state(session_id, StateKey.GIT_HEAD_ANNOUNCED, None)
    return raw if isinstance(raw, str) and raw.strip() else None


def record_announced_git_head(session_id: str, token: str) -> None:
    """Remember the token the agent has been told about (mem + durable mirror)."""
    from runtime import state_register_db
    from runtime.session.state_register import state_register_mem

    state_register_mem.set_state(session_id, StateKey.GIT_HEAD_ANNOUNCED, token)
    state_register_db.set_state(session_id, StateKey.GIT_HEAD_ANNOUNCED, token)


def git_head_notice_text(
    previous: str, current: str, directory: str, *, switched: bool = False
) -> str:
    """The body of a workspace-checkpoint notice.

    The root is always named: a notice may follow a directory switch, and a
    pair of bare checkpoint tokens alone would not say WHICH tree they describe.
    ``switched`` picks the wording — after a directory switch the previous token
    came from ANOTHER project, so "moved from" would be a lie.
    """
    if switched:
        return (
            f"[Workspace checkpoint / 工作区快照] The working directory moved to `{directory}`, "
            f"whose shadow checkpoint is `{current}` (the previous tree was on `{previous}`). "
            "Re-read the files you were working from: they came from a different tree state."
        )
    return (
        f"[Workspace checkpoint changed / 工作区快照已变化] The project at `{directory}` moved "
        f"from `{previous}` to `{current}`. Re-check the files before editing: the working tree "
        "now holds a different state than the one your earlier reads and edits were made against."
    )


def prime_git_head_from_store() -> int:
    """Warm the mem tier with the persisted checkpoint baselines (called once at boot).

    A lost baseline would silently re-baseline on the next message, and the agent
    would never hear about a branch change that predates the restart. Failures are
    swallowed — a broken mirror must not stop the boot.

    :returns: How many sessions were warmed.
    """
    from runtime import state_register_db
    from runtime.session.state_register import state_register_mem

    warmed = 0
    try:
        for session_id in state_register_db.get_all_session_ids():
            if state_register_mem.get_state(session_id, StateKey.GIT_HEAD_ANNOUNCED, None):
                continue
            announced = state_register_db.get_state(session_id, StateKey.GIT_HEAD_ANNOUNCED, None)
            if isinstance(announced, str) and announced.strip():
                state_register_mem.set_state(session_id, StateKey.GIT_HEAD_ANNOUNCED, announced)
                warmed += 1
    except Exception as exc:  # noqa: BLE001 - boot must not depend on the mirror
        from loguru import logger

        logger.warning("git_head: failed to prime the mem tier from the store: {}", exc)
        return warmed
    if warmed:
        from loguru import logger

        logger.info("git_head: primed {} session baseline(s) from the durable store", warmed)
    return warmed
