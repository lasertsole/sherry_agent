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
    "max_snapshots": 20,
    "git_timeout_s": 30.0,
    "store_dir": "",
}
