"""Shadow-git snapshot limits (per project)."""

from typing import TypedDict


class ShadowGitConfig(TypedDict):
    """Limits for the per-project shadow git store."""

    enabled: bool
    max_snapshots: int
    max_total_size_mb: int
    max_file_size_mb: int
    git_timeout_s: float
    gc_interval_turns: int


SHADOW_GIT: ShadowGitConfig = {
    "enabled": True,
    "max_snapshots": 20,
    "max_total_size_mb": 500,
    "max_file_size_mb": 10,
    "git_timeout_s": 30.0,
    "gc_interval_turns": 50,
}
