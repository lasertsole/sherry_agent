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
    # The store is PER PROJECT ROOT and shared by every session (subagents
    # included), so a small budget was consumed in one or two rounds of a
    # concurrent fan-out and the restore window collapsed to the last step.
    # ``ensure_checkpoint`` dedupes per session per turn, so 100 covers ~100
    # turns in one session or ten children at ten turns each — far beyond the
    # handful of steps a rollback ever needs. Storage stays small: git objects
    # are deduplicated and delta-compressed, and the store is bare (objects +
    # refs, no worktree).
    "max_snapshots": 100,
    "git_timeout_s": 30.0,
    "store_dir": "",
}
