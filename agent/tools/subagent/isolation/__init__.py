"""Isolated subagent workspaces: a private copy of the project, merged on completion.

A subagent normally works directly in the parent's project tree, so two children
editing overlapping files collide (the write locks and the read-before-write
license refuse the loser instead of losing its edit, but the loser still loses
its turn). This package is the opt-in alternative, modelled on omo's
``isolation-core``: the child gets its own **git worktree** of the project,
cut from a dirty baseline (uncommitted edits included) with every ignored path
materialized back in as a link or a copy, works there end to end, and on
completion its changes are merged back into the parent tree under a lock, file
by file, with the same revision CAS the file tools use — a file the parent moved
in the meantime is a reported CONFLICT, not a silent overwrite.

Layout of one isolated workspace (under ``config.path.isolated_workspaces_dir``)::

    <slug>/
        snapshot.json   # parent root, baseline revision, branch, manifest
        tree/           # the child's project directory (a git worktree)

Opt-in: ``sessions_spawn(isolation=True)``. Nothing else changes for a spawn
that does not ask for it.
"""

from .merge import MergeReport, merge_isolated_workspace
from .tree import (
    IsolatedWorkspace,
    create_isolated_workspace,
    discard_isolated_workspace,
    isolated_workspace_meta,
    materialized_paths,
)

__all__ = [
    "IsolatedWorkspace",
    "MergeReport",
    "create_isolated_workspace",
    "discard_isolated_workspace",
    "isolated_workspace_meta",
    "materialized_paths",
    "merge_isolated_workspace",
]
