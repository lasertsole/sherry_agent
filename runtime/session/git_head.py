"""Git branch/HEAD of a session's working root — read, remembered, announced.

The workspace notice middleware needs one fact at the start of every turn: which
branch and HEAD the session's project directory is on RIGHT NOW, so it can tell
the agent when that moved (the user switched branches in the sidebar's git
panel, ran a checkout in a terminal, or committed outside the session).

This module is the shared leaf for that fact, mirroring
``runtime.session.project_dir``: the reader is mem-only and cheap for the agent
side (``read_git_head`` runs one bounded git command), while the "what the agent
was told" baseline lives in the session state register under
``StateKey.GIT_HEAD_ANNOUNCED`` with a durable mirror that the boot prime warms.

Why a token rather than two fields: the comparison is equality-only — a notice
is owed exactly when the (branch, hash) PAIR differs from the announced pair, so
one string keeps the comparison and the stored value identical. A detached HEAD
is not special-cased: git answers ``HEAD`` as the branch name and the hash still
differs when the user checks out another commit, which is precisely the change
the agent must hear about.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from config.features import GIT_GRAPH
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

#: Metadata carried by the git-change notice (the shared workspace-notice shape).
GIT_HEAD_NOTICE_METADATA = workspace_notice_metadata(GIT_HEAD_NOTICE_ORIGIN)

#: Characters of the commit hash kept in the token (a row label, not an id).
_SHORT_HASH_CHARS = 8


def read_git_head(directory: str | Path) -> str | None:
    """The root's git branch + HEAD as ``<branch>@<short-hash>``; ``None`` if none.

    One bounded ``git rev-parse`` answers both facts. ``None`` covers every
    "there is nothing to announce" case — no git binary, not a repository, a
    repository without commits (git fails), or a call that timed out — so the
    caller never needs to distinguish them: a missing token simply means no
    notice.

    Blocking (a subprocess, measured ~14 ms locally); the caller runs it once
    per turn at send time, the same budget class as the state reads beside it.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD", "--abbrev-ref", "HEAD"],
            cwd=str(directory),
            capture_output=True,
            text=True,
            timeout=GIT_GRAPH["timeout_s"],
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2 or not lines[0] or not lines[1]:
        return None
    return f"{lines[1]}@{lines[0][:_SHORT_HASH_CHARS]}"


def announced_git_head(session_id: str | None) -> str | None:
    """The branch/HEAD token the agent was last TOLD about (mem tier).

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
    """The body of a git-change notice.

    The root is always named: a notice may follow a directory switch, and
    "``main@1a2b3c4d`` → ``dev@5e6f7a8b``" alone would not say WHICH tree it is
    talking about. ``switched`` picks the wording — after a directory switch the
    previous token came from ANOTHER repository, so "moved from" would be a lie.
    """
    if switched:
        return (
            f"[Git 分支/HEAD / git branch] The working directory moved to `{directory}`, whose "
            f"repository is on `{current}` (the previous tree was on `{previous}`). Re-read the "
            "files you were working from: they came from a different branch or revision."
        )
    return (
        f"[Git 分支/HEAD 已变化 / git branch changed] The repository at `{directory}` moved from "
        f"`{previous}` to `{current}`. Re-check the checked-out files before editing: the working "
        "tree now holds a different branch or revision than the one your earlier reads and edits "
        "were made against."
    )


def prime_git_head_from_store() -> int:
    """Warm the mem tier with the persisted git baselines (called once at boot).

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
