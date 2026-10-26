"""Shadow-git snapshot limits (per project)."""

from typing import TypedDict


class ShadowGitConfig(TypedDict):
    """Limits for the per-project shadow git store."""

    enabled: bool
    max_snapshots: int
    git_timeout_s: float


SHADOW_GIT: ShadowGitConfig = {
    "enabled": True,
    "max_snapshots": 20,
    "git_timeout_s": 30.0,
}
