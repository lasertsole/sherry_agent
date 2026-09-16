"""Shared path resolution utilities for file tools."""

import errno
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


# ── Path safety gates ───────────────────────────────────────────────

_WIN32_ERROR_CANT_RESOLVE_FILENAME = 1921


def _reject_traversal_input(file_path: str) -> None:
    """Gate 1: string-level rejection of traversal, before any filesystem I/O.

    Rejects ``~``-prefixed input and any ``..`` path *component*. Component
    matching (``Path(...).parts``) is deliberate: a substring test would
    false-positive on legitimate names such as ``foo..bar`` or ``配置..md``.
    """
    if file_path.startswith("~") or any(part == ".." for part in Path(file_path).parts):
        raise PathOutOfBoundsError(f"Path traversal not allowed: {file_path}")


def _is_eloop_oserror(exc: BaseException | None) -> bool:
    """Return True when exc is an OS-level symlink-loop (ELOOP) error."""
    return isinstance(exc, OSError) and (
        exc.errno == errno.ELOOP
        or getattr(exc, "winerror", None) == _WIN32_ERROR_CANT_RESOLVE_FILENAME
    )


def _is_symlink_loop_error(exc: Exception) -> bool:
    """Return True when exc (or a chained cause/context) signals a symlink loop."""
    if _is_eloop_oserror(exc):
        return True
    return isinstance(exc, RuntimeError) and any(
        _is_eloop_oserror(chained) for chained in (exc.__cause__, exc.__context__)
    )


def _raise_if_symlink_loop(path: Path) -> None:
    """Gate 3: raise ELOOP when ``path`` is itself a looping symlink.

    ``Path.resolve()`` stops silently at a symlink loop and hands back the
    looping link, which would later fail in confusing ways. ``stat()`` maps
    that condition to ``OSError(ELOOP)``, turning it into an explicit error.
    """
    if not path.is_symlink():
        return
    try:
        path.stat()
    except OSError as exc:
        if _is_eloop_oserror(exc):
            raise


def _open_no_follow(path: Path, flags: int, mode: int = 0o644) -> int:
    """``os.open`` with ``O_NOFOLLOW``; Windows fallback: an ``is_symlink`` check.

    Closes the TOCTOU window between resolution and I/O: the final path
    component is refused when it is a symlink, so a link swapped in after
    validation cannot redirect the read/write outside ROOT_DIR. Raises
    ``OSError(ELOOP)`` — the same errno Linux/macOS produce natively.
    """
    if not hasattr(os, "O_NOFOLLOW"):
        if path.is_symlink():
            raise OSError(errno.ELOOP, "Symbolic link not allowed")
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)


def resolve_project_path(file_path: str) -> Path:
    """Resolve file_path against ROOT_DIR; reject paths escaping the project.

    Three gates run in order:

    1. String-level rejection of ``..`` components and ``~`` prefixes
       (no filesystem access).
    2. ``resolve()`` + ``relative_to(ROOT_DIR)`` containment — absolute
       paths that resolve outside are rejected via
       :class:`PathOutOfBoundsError`.
    3. Symlink-loop detection on the resolved path (``OSError(ELOOP)``).

    Relative paths are joined onto ROOT_DIR. For paths that legitimately
    need to reach outside ROOT_DIR, use :func:`resolve_external_path` instead.
    """
    _reject_traversal_input(file_path)
    p = Path(os.path.expanduser(file_path))
    if not p.is_absolute():
        p = ROOT_DIR / p
    resolved = p.resolve()
    if resolved != ROOT_DIR and not resolved.is_relative_to(ROOT_DIR):
        raise PathOutOfBoundsError(
            f"Path resolves outside project root and is not allowed: {resolved} (root={ROOT_DIR})"
        )
    _raise_if_symlink_loop(resolved)
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


# ── Model-visible path rendering ────────────────────────────────────


def to_virtual_path(real_path: Path) -> str:
    """Convert a real filesystem path to a virtual path anchored at ROOT_DIR.

    /home/user/project/src/main.py -> /src/main.py

    Raises ValueError if the path is outside ROOT_DIR.
    """
    return "/" + real_path.resolve().relative_to(ROOT_DIR.resolve()).as_posix()


def display_path(real_path: Path) -> str:
    """Safely render a path for model-visible output.

    Returns a virtual path in normal cases. If the path cannot be converted
    (outside root, unresolvable symlink), falls back to just the filename
    so ROOT_DIR never leaks.
    """
    try:
        return to_virtual_path(real_path)
    except (ValueError, OSError, RuntimeError):
        return real_path.name or "/"


def safe_error_detail(exc: Exception) -> str:
    """Extract an agent-safe error detail string.

    ``OSError.__str__`` embeds the real file path, so those surface only
    ``strerror`` ('Permission denied', path-free); ``UnicodeDecodeError``
    exposes ``.reason`` ('invalid start byte'). Every other exception's
    message is deliberately DROPPED — generic exception text can still embed
    the real root path (for example an error raised mid-``Path.rglob``), and
    this module is always "virtual mode": ROOT_DIR must never leak into
    agent-visible output. Only the exception type name survives.

    Mirrors deepagents ``_safe_detail`` under ``virtual_mode=True``
    (`backends/filesystem.py:1201-1214`), which likewise never falls back to
    ``str(exc)``.
    """
    if isinstance(exc, OSError):
        detail = exc.strerror
    else:
        detail = getattr(exc, "reason", None)
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


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


def _get_yolo_deny_paths() -> list[str]:
    """Return the YOLO deny list: config defaults + user entries from sherry.jsonc.

    Defaults come first, user additions follow; duplicates are dropped so the
    order stays stable and the first occurrence wins.
    """
    from config.features import HITL_DEFAULTS
    from config.sherry_settings import load_sherry_list_setting

    merged: list[str] = []
    for pattern in (
        *HITL_DEFAULTS.get("yolo_deny_paths", []),
        *load_sherry_list_setting("yolo_deny_paths"),
    ):
        if isinstance(pattern, str) and pattern.strip() and pattern not in merged:
            merged.append(pattern)
    return merged


def _is_yolo_denied(resolved: Path) -> bool:
    """Check whether resolved is on the YOLO deny list.

    Always enforced — even when YOLO is active, the allowlist matches, or a
    subagent inherits its parent's authorization. ``~`` is expanded at check
    time; a pattern with a trailing separator matches that directory and every
    descendant, a pattern without one is an exact match.
    """
    resolved_str = str(resolved)
    for pattern in _get_yolo_deny_paths():
        if not isinstance(pattern, str) or not pattern.strip():
            continue
        raw = os.path.expanduser(pattern.strip())
        is_dir = raw.endswith(("/", "\\"))
        try:
            expanded = Path(raw).resolve()
            if is_dir:
                if resolved.is_relative_to(expanded):
                    return True
            elif resolved_str == str(expanded):
                return True
        except (TypeError, ValueError):
            continue
    return False


def _check_allowlist(resolved: Path, session_id: str) -> bool:
    """Check if resolved path is in the session-level allowlist.

    Checks session_id (self), requester_session_key (parent), and global.
    A directory entry (trailing separator) matches the directory and every
    path beneath it; any other entry is an exact path match.
    """
    from runtime import state_register_mem

    resolved_str = str(resolved)
    for sid in _candidate_session_ids(session_id):
        entries = state_register_mem.get_state(sid, _ALLOWLIST_KEY, [])
        if not entries:
            continue
        for entry in entries:
            if resolved_str == entry:
                return True
            if isinstance(entry, str) and entry.endswith(("/", "\\")):
                try:
                    if resolved.is_relative_to(Path(entry)):
                        return True
                except (TypeError, ValueError):
                    continue
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


def _add_to_allowlist(resolved: Path, session_id: str, *, mode: str = "file") -> None:
    """Add a resolved path to the session allowlist.

    ``mode="file"`` stores the exact path; ``mode="dir"`` stores a normalized
    directory entry (trailing separator) matching the directory and every
    descendant. Re-adding an existing entry is a no-op.
    """
    from runtime import state_register_mem

    entries = state_register_mem.get_state(session_id, _ALLOWLIST_KEY, [])
    path_str = str(resolved).rstrip("/\\") + "/" if mode == "dir" else str(resolved)
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
    2. YOLO deny list → deny (security floor: checked before YOLO and allowlist)
    3. YOLO flag (state_register_db) → return
    4. Session allowlist (exact or directory-prefix match) → return
    5. Subagent without prior auth → deny (cannot self-approve)
    6. Main agent → HITL interrupt (approve / approve_dir / yolo / reject)

    Args:
        file_path: The path to resolve (relative, absolute, or ~).
        session_id: Current session ID (main or child).
        action_desc: Optional description for the approval prompt.

    Raises:
        PathOutOfBoundsError: If denied by the deny list, by user, or for a
            subagent without prior authorization.
    """
    p = Path(os.path.expanduser(file_path))
    if not p.is_absolute():
        p = ROOT_DIR / p
    resolved = p.resolve()

    # 1. Inside ROOT_DIR — safe path, no approval
    if resolved == ROOT_DIR or resolved.is_relative_to(ROOT_DIR):
        return resolved

    # 2. YOLO deny list — always enforced, even when YOLO/allowlist would allow
    if _is_yolo_denied(resolved):
        raise PathOutOfBoundsError(
            f"Path is in the YOLO deny list and cannot be accessed: {resolved}"
        )

    # 3. YOLO — persistent global allow-all
    if _is_yolo():
        return resolved

    # 4. Session allowlist — exact or directory-prefix match, inherited by subagents
    if _check_allowlist(resolved, session_id):
        return resolved

    # 5. Subagent without prior authorization — cannot self-approve
    if _is_subagent(session_id):
        raise PathOutOfBoundsError(
            f"External path not authorized for subagent: {resolved}. "
            f"Approve this path from the main session first."
        )

    # 6. Main session — trigger HITL interrupt
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
            f"External file access approval\n"
            f"  Path: {resolved}\n"
            f"  Project root: {ROOT_DIR}\n"
            f"  Intent: {action_desc or 'unspecified'}\n\n"
            f"Options:\n"
            f"  approve      — allow this file only (session-scoped, inherited by subagents)\n"
            f"  approve_dir  — allow entire directory {resolved.parent}/ (session-scoped, "
            f"inherited by subagents)\n"
            f"  yolo         — permanently allow all external paths (no more prompts)\n"
            f"  reject       — deny"
        ),
    )
    review_config = ReviewConfig(
        action_name="external_file_access",
        allowed_decisions=["approve", "approve_dir", "yolo", "reject"],
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
        _add_to_allowlist(resolved, session_id, mode="file")
        return resolved

    if decision_type == "approve_dir":
        # Session-scoped: add the parent directory (prefix match covers children)
        _add_to_allowlist(resolved.parent, session_id, mode="dir")
        return resolved

    if decision_type == "yolo":
        # Persistent global allow-all
        from runtime import state_register_db

        state_register_db.set_state(_GLOBAL_SESSION, _YOLO_KEY, True)
        return resolved

    # Reject
    msg = decisions[0].get("message", "Rejected by user")
    raise PathOutOfBoundsError(f"External file access denied: {resolved} ({msg})")
