"""Read-only access to a project's shadow-git HEAD.

This is the dependency-free seam shared by ``runtime`` (workspace notices) and
``agent`` (shadow-git tooling): both may import it, while it only depends on
``config``. It deliberately does NOT initialize or mutate the shadow store.

The store is GLOBAL (``APP_HOME/checkpoints/store`` by default) with one branch
ref per project: ``refs/sherry/<sha256(abs_path)[:16]>``. ``SHADOW_REF`` is kept
as a compatibility alias only; no git operation uses it any more.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from config.features import SHADOW_GIT
from config.path import APP_HOME

__all__ = [
    "SHADOW_REF",
    "shadow_hash",
    "shadow_head",
    "shadow_ref",
    "shadow_store_root",
]

SHADOW_REF = "refs/heads/main"


def shadow_store_root() -> Path:
    """The shared bare store root (override, else ``APP_HOME/checkpoints/store``)."""
    override = str(SHADOW_GIT.get("store_dir") or os.getenv("SHERRY_SHADOW_GIT_DIR", "")).strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(APP_HOME) / "checkpoints" / "store"


def shadow_hash(project_root: Path) -> str:
    """``sha256(normalized absolute project path)[:16]`` (hermes-aligned)."""
    normalised = str(Path(project_root).resolve())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


def shadow_ref(project_root: Path) -> str:
    """The per-project branch ref inside the shared store."""
    return f"refs/sherry/{shadow_hash(project_root)}"


def shadow_head(project_root: Path) -> str | None:
    """The latest snapshot hash for THIS project, without creating the store.

    A shared store's ``HEAD`` exists as soon as ANY project snapshots, so its
    presence is not the per-project test. The project has snapshots only when
    its own ``refs/sherry/<hash>`` resolves; a missing/foreign ref is ``None``.
    """
    store = shadow_store_root()
    if not (store / "HEAD").exists():
        return None
    ref = shadow_ref(project_root)
    try:
        verify = subprocess.run(
            ["git", "--git-dir", str(store), "show-ref", "--verify", "--quiet", ref],
            capture_output=True,
            text=True,
            timeout=SHADOW_GIT["git_timeout_s"],
        )
        if verify.returncode != 0:
            return None
        result = subprocess.run(
            ["git", "--git-dir", str(store), "rev-parse", "--short", ref],
            capture_output=True,
            text=True,
            timeout=SHADOW_GIT["git_timeout_s"],
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None
