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
