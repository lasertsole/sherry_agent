"""Shadow-git snapshot limits (per project)."""

from typing import TypedDict


class ShadowGitConfig(TypedDict):
    """Limits for the per-project shadow git store."""

    enabled: bool
    max_snapshots: int
    git_timeout_s: float
    #: Optional store root. Empty keeps the historical
    #: ``<project>/.sherry/shadow-git``; the eval harness points this at a
    #: run-scoped directory so the instance repository never contains agent
    #: private state.
    store_dir: str


SHADOW_GIT: ShadowGitConfig = {
    "enabled": True,
    # A SOFT cap on HISTORY. The store is PER PROJECT ROOT and shared by every
    # session (subagents included), so a small budget was consumed in one or
    # two rounds of a concurrent fan-out and the restore window collapsed to
    # the last step. The cap alone would then also delete the boundary an
    # edit-message rewind needs: a single turn can add more than 100 commits
    # (one per fan-out child, plus the restore pair), so pruning NEVER crosses
    # a turn floor (``shadow_git.floor_for``). The effective window is
    # ``max(max_snapshots, everything since the oldest ACTIVE turn floor)``;
    # once the turn ends its floor stops matching and the excess is released.
    # Storage stays small: git objects are deduplicated and delta-compressed,
    # and the store is bare (objects + refs, no worktree).
    "max_snapshots": 100,
    "git_timeout_s": 30.0,
    "store_dir": "",
}
