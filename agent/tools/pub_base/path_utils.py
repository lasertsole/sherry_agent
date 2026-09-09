"""Shared path resolution utilities for file tools."""

import os
from pathlib import Path

from config import ROOT_DIR


class PathOutOfBoundsError(ValueError):
    """Raised when a resolved path escapes ROOT_DIR.

    File tools must NOT be able to read/write outside the project root;
    otherwise an LLM-triggered model can exfiltrate secrets (e.g. .env),
    overwrite arbitrary files, or read system paths. Absolute paths that
    resolve outside ROOT_DIR are rejected rather than silently allowed.
    """


def resolve_project_path(file_path: str) -> Path:
    """Resolve file_path against ROOT_DIR; reject paths escaping the project.

    Relative paths are joined onto ROOT_DIR; ~ is expanded. The result is
    guaranteed to be ROOT_DIR or a descendant thereof — absolute paths that
    resolve outside are rejected via :class:`PathOutOfBoundsError`.

    For paths that legitimately need to reach outside ROOT_DIR, use
    :func:`resolve_external_path` instead.
    """
    p = Path(os.path.expanduser(file_path))
    if not p.is_absolute():
        p = ROOT_DIR / p
    resolved = p.resolve()
    if resolved != ROOT_DIR and not resolved.is_relative_to(ROOT_DIR):
        raise PathOutOfBoundsError(
            f"Path resolves outside project root and is not allowed: {resolved} (root={ROOT_DIR})"
        )
    return resolved


def resolve_path(file_path: str) -> Path:
    """Deprecated alias for resolve_project_path."""
    import warnings

    warnings.warn(
        "resolve_path is deprecated; use resolve_project_path",
        DeprecationWarning,
        stacklevel=2,
    )
    return resolve_project_path(file_path)


# ── State keys ──────────────────────────────────────────────────────

_GLOBAL_SESSION = "__global__"
_YOLO_KEY = "external_path_yolo"
_ALLOWLIST_KEY = "external_path_allowlist"


def _extract_session_id(run_manager) -> str:
    """Extract session_id from CallbackManagerForToolRun config.

    Returns empty string when unavailable — callers must treat empty
    as fail-closed (no HITL approval possible).
    """
    config = getattr(run_manager, "config", None) or {}
    configurable = config.get("configurable", {})
    return configurable.get("session_id", "")


def _is_yolo() -> bool:
    """Check persistent YOLO flag from state_register_db."""
    from runtime import state_register_db

    return bool(state_register_db.get_state(_GLOBAL_SESSION, _YOLO_KEY, False))


def _check_allowlist(resolved: Path, session_id: str) -> bool:
    """Check if resolved path is in the session-level allowlist (exact match).

    Checks session_id (self), requester_session_key (parent), and global.
    Exact match only — no directory-level inheritance.
    """
    from runtime import state_register_mem

    for sid in _candidate_session_ids(session_id):
        entries = state_register_mem.get_state(sid, _ALLOWLIST_KEY, [])
        if not entries:
            continue
        for entry in entries:
            if str(resolved) == entry:
                return True
    return False


def _candidate_session_ids(session_id: str) -> list[str]:
    """Return [session_id, requester_session_key, _GLOBAL_SESSION].

    The requester_session_key is looked up from state_register_mem under
    the child's own session_id. Subagents inherit the parent's allowlist.
    """
    from runtime import state_register_mem

    ids = [session_id, _GLOBAL_SESSION]
    requester = state_register_mem.get_state(session_id, "requester_session_key", "")
    if requester and requester not in ids:
        ids.insert(1, requester)
    return ids


def _add_to_allowlist(resolved: Path, session_id: str) -> None:
    """Add exact resolved path to the session allowlist (no parent dir)."""
    from runtime import state_register_mem

    entries = state_register_mem.get_state(session_id, _ALLOWLIST_KEY, [])
    path_str = str(resolved)
    if path_str not in entries:
        entries.append(path_str)
        state_register_mem.set_state(session_id, _ALLOWLIST_KEY, entries)


def _is_subagent(session_id: str) -> bool:
    """Check if the current session is a subagent by looking up caller_scope."""
    from runtime import state_register_mem

    if not session_id:
        return False
    scope = state_register_mem.get_state(session_id, "caller_scope", "main")
    return scope == "subagent"


def resolve_external_path(
    file_path: str,
    *,
    session_id: str,
    action_desc: str = "",
) -> Path:
    """Resolve a path that may be outside ROOT_DIR, gated by HITL approval.

    Checks (in order):
    1. Inside ROOT_DIR → return directly (safe path)
    2. YOLO flag (state_register_db) → return
    3. Session allowlist (state_register_mem, exact match) → return
    4. Subagent without prior auth → deny (cannot self-approve)
    5. Main agent → HITL interrupt (approve / yolo / reject)

    Args:
        file_path: The path to resolve (relative, absolute, or ~).
        session_id: Current session ID (main or child).
        action_desc: Optional description for the approval prompt.

    Raises:
        PathOutOfBoundsError: If denied by user or subagent without prior auth.
    """
    p = Path(os.path.expanduser(file_path))
    if not p.is_absolute():
        p = ROOT_DIR / p
    resolved = p.resolve()

    # 1. Inside ROOT_DIR — safe path, no approval
    if resolved == ROOT_DIR or resolved.is_relative_to(ROOT_DIR):
        return resolved

    # 2. YOLO — persistent global allow-all
    if _is_yolo():
        return resolved

    # 3. Session allowlist — exact match, inherited by subagents
    if _check_allowlist(resolved, session_id):
        return resolved

    # 4. Subagent without prior authorization — cannot self-approve
    if _is_subagent(session_id):
        raise PathOutOfBoundsError(
            f"External path not authorized for subagent: {resolved}. "
            f"Approve this path from the main session first."
        )

    # 5. Main session — trigger HITL interrupt
    from langchain.agents.middleware.human_in_the_loop import (
        ActionRequest,
        HITLRequest,
        ReviewConfig,
    )
    from langgraph.types import interrupt

    action_request = ActionRequest(
        name="external_file_access",
        args={"path": str(resolved)},
        description=(
            f"外部文件访问审批\n"
            f"  路径: {resolved}\n"
            f"  项目根: {ROOT_DIR}\n"
            f"  意图: {action_desc or '未指定'}\n\n"
            f"选项:\n"
            f"  approve — 允许（本次会话有效，子 agent 沿用）\n"
            f"  yolo    — 永久允许所有外部路径（不再弹窗）\n"
            f"  reject  — 拒绝"
        ),
    )
    review_config = ReviewConfig(
        action_name="external_file_access",
        allowed_decisions=["approve", "yolo", "reject"],
    )

    hitl_response = interrupt(
        HITLRequest(
            action_requests=[action_request],
            review_configs=[review_config],
        )
    )

    decisions = hitl_response.get("decisions", [])
    if not decisions:
        raise PathOutOfBoundsError(f"External access denied (no decision): {resolved}")

    decision_type = decisions[0].get("type", "")

    if decision_type == "approve":
        # Session-scoped: add exact path to allowlist
        _add_to_allowlist(resolved, session_id)
        return resolved

    if decision_type == "yolo":
        # Persistent global allow-all
        from runtime import state_register_db

        state_register_db.set_state(_GLOBAL_SESSION, _YOLO_KEY, True)
        return resolved

    # Reject
    msg = decisions[0].get("message", "Rejected by user")
    raise PathOutOfBoundsError(f"External file access denied: {resolved} ({msg})")
