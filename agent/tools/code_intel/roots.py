"""Index-root resolution for the code-intelligence tools.

A leaf module: both the four query tools and the semantic search tool resolve
their root through :func:`root_for_call`, and neither may import the other's
module at import time.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Explicit sandbox override (tests / drills): pins the root for the process.
ROOT_ENV_KEY = "SHERRY_CODE_INTEL_ROOT"

#: Explicit index-database override.
DB_ENV_KEY = "SHERRY_CODE_INTEL_DB"


def index_db_for_root(root: Path | None, configured: str, default_dir: Path) -> str:
    """The index DB for one call's root: explicit/env > per-project > shared.

    A project gets its OWN ``<root>/.codeintel/index.db`` so switching between
    projects no longer rebuilds one shared index (the relative paths in
    ``index_meta`` collide across roots). The shared ``CODE_INTEL_DIR`` remains
    the fallback for a call with no project root — and for an existing shared
    index, so nothing is silently re-indexed after this change.
    """
    override = os.environ.get(DB_ENV_KEY, "").strip()
    if override:
        return str(Path(override).expanduser())
    if configured:
        return configured
    if root is not None and root.is_dir():
        return str(Path(root) / ".codeintel" / "index.db")
    return str(default_dir / "index.db")


def resolve_root() -> Path:
    """Resolve the build-time index root: env override, else the process cwd."""
    override = os.environ.get(ROOT_ENV_KEY, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.cwd().resolve()


def root_for_call(explicit: Path | None, session_id: str, fallback: Path) -> Path:
    """The root THIS call queries.

    Read per call, never cached on the tool: a session switches its project
    directory at a turn boundary, and a spawned child is frozen to the root it
    was spawned with. Mirrors ``_LspTool._session_root`` / the ast-grep runner.
    Precedence: an explicit build-time root (tests/drills) >
    ``SHERRY_CODE_INTEL_ROOT`` > the session's project directory > the process
    default.
    """
    if explicit is not None:
        return explicit
    if os.environ.get(ROOT_ENV_KEY, "").strip():
        return fallback
    from agent.tools.pub_base import session_workspace_root

    bound = session_workspace_root(session_id)
    return Path(bound).resolve() if bound is not None else fallback
