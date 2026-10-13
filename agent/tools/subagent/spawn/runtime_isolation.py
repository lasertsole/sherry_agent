"""Runtime isolation and security boundaries for sub-agents.

Enforces CWD restrictions and cross-runtime spawn prevention (without
porting the full ACP sandbox).
"""

from loguru import logger
from pydantic import BaseModel
from config import ROOT_DIR


class RuntimeIsolationConfig(BaseModel):
    """Isolation configuration: allowed CWD prefixes and restricted-mode flag."""

    runtime: str = "subagent"
    allowed_cwd_prefixes: list[str] = []
    restricted: bool = False


def resolve_runtime_isolation(
    requester_session_key: str,
    agent_id: str = "main",
    cwd: str | None = None,
) -> RuntimeIsolationConfig:
    """Build an isolation config for the child, marking cross-runtime spawns as restricted."""
    config = RuntimeIsolationConfig()

    if cwd:
        config.allowed_cwd_prefixes = [cwd]

    if agent_id != "main" and agent_id != config.runtime:
        config.restricted = True
        logger.warning(
            "Runtime isolation: cross-runtime spawn from {} to {}", requester_session_key, agent_id
        )

    return config


def resolve_spawned_workspace_inheritance(
    requester_session_key: str,
    target_agent_id: str,
    requester_cwd: str | None = None,
) -> str | None:
    """Determine the working directory for the child.

    Precedence: an explicit ``requester_cwd`` (the spawn ``cwd`` argument) → the
    **parent session's project directory** → a per-agent ``workspaces/<id>``
    directory when one exists (the legacy probe; nothing in the repo creates
    that tree, so it is effectively a last resort). ``None`` means "no opinion":
    the child falls back to the process default exactly as before.

    The parent's directory is read ONCE here, at spawn time: a child runs with
    the root it was spawned with and does NOT follow a later switch in the
    parent session (the ``spawned_cwd`` freeze, matching the "children are
    independent sessions" rule).
    """
    if requester_cwd:
        return requester_cwd

    from agent.tools.pub_base import session_workspace_root
    from agent.tools.subagent.registry.session_keys import normalize_session_key

    parent_root = session_workspace_root(normalize_session_key(requester_session_key))
    if parent_root is not None:
        return str(parent_root)

    parts = requester_session_key.split(":")
    if len(parts) >= 3:
        workspace_hint = ROOT_DIR / "workspaces" / target_agent_id
        if workspace_hint.is_dir():
            return str(workspace_hint)

    return None


def validate_runtime_isolation(config: RuntimeIsolationConfig) -> tuple[bool, str]:
    """Reject restricted (cross-runtime) spawn configurations."""
    if config.restricted:
        return False, f"Cross-runtime spawn not allowed (runtime={config.runtime})"

    return True, ""


def validate_cwd_restriction(cwd: str | None, allowed_prefixes: list[str]) -> tuple[bool, str]:
    """Ensure the requested CWD falls under one of the allowed workspace prefixes."""
    if not cwd or not allowed_prefixes:
        return True, ""

    from pathlib import Path

    cwd_path = Path(cwd).resolve()

    for prefix in allowed_prefixes:
        try:
            cwd_path.relative_to(Path(prefix).resolve())
            return True, ""
        except ValueError:
            continue

    return False, f"cwd '{cwd}' is outside allowed workspace"
