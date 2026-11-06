"""Shadow-git snapshot limits (one chain per project, one shared store)."""

from typing import TypedDict


class ShadowGitConfig(TypedDict):
    """Limits for the shared shadow-git store."""

    enabled: bool
    max_snapshots: int
    git_timeout_s: float
    #: Optional shared store root. Empty uses ``APP_HOME/checkpoints/store``;
    #: the eval harness points this at a run-scoped directory so the instance
    #: repository never contains agent-private state.
    store_dir: str
    #: Import a legacy ``<project>/.sherry/shadow-git`` into the shared store on
    #: the first snapshot (the legacy directory is renamed, never deleted).
    migrate_legacy: bool


SHADOW_GIT: ShadowGitConfig = {
    "enabled": True,
    # A SOFT cap on one project's HISTORY. The chain is per project root and
    # shared by every session (subagents included), so a small budget was
    # consumed in one or two rounds of a concurrent fan-out and the restore
    # window collapsed to the last step. The cap alone would then also delete
    # the boundary an edit-message rewind needs: a single turn can add more than
    # 100 commits (one per fan-out child, plus the restore pair), so pruning
    # NEVER crosses a turn floor (``shadow_git.floor_for``). The effective window
    # is ``max(max_snapshots, everything since the oldest ACTIVE turn floor)``;
    # once the turn ends its floor stops matching and the excess is released.
    # Storage stays small: git objects are shared and delta-compressed across
    # projects, and the store is bare (objects + refs, no worktree).
    "max_snapshots": 100,
    "git_timeout_s": 30.0,
    "store_dir": "",
    "migrate_legacy": True,
}
