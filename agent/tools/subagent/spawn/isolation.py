"""Opt-in workspace isolation for a spawned child (phase 4.1).

An isolated child works on a private COPY of the project directory; the
completion merge carries its changes back. Everything downstream (attachments,
the child's binding, its tools) sees the copy, so nothing else needs to know
isolation happened.
"""

from pathlib import Path
from typing import Any

from loguru import logger

from .validation import SpawnResult

#: Appended to an isolated child's system prompt: it works on a copy, and the
#: completion merge is what carries its changes back.
_ISOLATION_PROMPT_NOTE = (
    "## Isolated Workspace\n"
    "You are working in a private COPY of the project directory. Your file changes "
    "are merged back into the original project when your run completes — a file the "
    "original changed in the meantime is reported as a conflict instead of being "
    "overwritten. Work as if this directory were the project; do not try to reach "
    "the original tree."
)


def _is_inside_isolated_worktree(path: str | None) -> bool:
    """True when *path* is inside an isolated worktree directory.

    Isolated worktrees live under ``isolated_workspaces_dir()/<slug>/tree/``, so
    a ``relative_to`` check suffices — no git probing (which would also depend
    on git being usable). A missing path is False.
    """
    if not path:
        return False
    from config.path import isolated_workspaces_dir

    try:
        Path(path).resolve().relative_to(isolated_workspaces_dir().resolve())
    except (OSError, ValueError):
        return False
    return True


def _create_isolated_child_workspace(
    isolation: bool, cwd: str | None, child_session_key: str
) -> tuple[str | None, Any, SpawnResult | None]:
    """Phase 4.1: give the child a private COPY of the project directory.

    Returns ``(cwd, isolated_workspace, refusal)``; everything downstream
    (attachments, the child's binding, its tools) sees the copy and nothing
    needs to know isolation happened.
    """
    if not isolation:
        return cwd, None, None

    # NESTED isolation is downgraded, not refused: a spawn whose cwd already sits
    # inside an isolated worktree reuses it. ``max_spawn_depth`` is 2, so that
    # cwd belongs to the requesting ORCHESTRATOR — its worktree already merges
    # back to the real project, and a worktree of a worktree would only add a
    # second merge-back, a second conflict surface and a second CAS check for no
    # additional isolation boundary. The model asked for isolation and gets it
    # (the child is isolated from the project); it just is not double-isolated.
    if _is_inside_isolated_worktree(cwd):
        logger.info(
            "spawn: isolation=True ignored for child {} — parent cwd {} is already an "
            "isolated worktree; the child reuses it",
            child_session_key,
            cwd,
        )
        return cwd, None, None

    if not cwd:
        return (
            cwd,
            None,
            SpawnResult(
                status="forbidden",
                error=(
                    "isolation requires a project directory to copy: the caller has "
                    "neither a bound project directory nor an explicit cwd"
                ),
            ),
        )
    from ..isolation import create_isolated_workspace

    try:
        isolated_workspace = create_isolated_workspace(Path(cwd), child_session_key)
    except OSError as exc:
        return cwd, None, SpawnResult(status="error", error=f"isolated workspace failed: {exc}")
    return str(isolated_workspace.tree), isolated_workspace, None
