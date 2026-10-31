"""Read-only access to a project's shadow-git HEAD.

This is the dependency-free seam shared by ``runtime`` (workspace notices) and
``agent`` (checkpoint tools): both may import it, while it only depends on
``config``. It deliberately does NOT initialize or mutate the shadow store.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from config.features import SHADOW_GIT

__all__ = ["SHADOW_REF", "shadow_head"]

SHADOW_REF = "refs/heads/main"


def shadow_head(project_root: Path) -> str | None:
    """The latest snapshot hash, WITHOUT creating a store on a read path."""
    root = project_root.resolve()
    store = root / ".sherry" / "shadow-git"
    if not (store / "HEAD").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "--git-dir", str(store), "rev-parse", "--short", SHADOW_REF],
            capture_output=True,
            text=True,
            timeout=SHADOW_GIT["git_timeout_s"],
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None
