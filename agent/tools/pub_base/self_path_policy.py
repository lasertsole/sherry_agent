"""Sherry's own files: per-mode allow-lists for the file tools and the shell screen.

The hole this closes: with nothing configured, a session's project directory IS
the checkout, so ``resolve_workspace_path``'s in-root test called the whole
source tree (and every credential file beside it) "safe" — ``patch_file`` could
rewrite ``agent/core.py`` and ``read_file`` could read ``.models`` (API keys) or
``src/data/auth.db`` (password hashes) with no approval.

The policy is a WHITELIST, and there are TWO of them because the criteria differ:

* **write** — "would changing it break the runtime, another session, or leak a
  credential". Only deliberately harmless directories qualify: the project's
  ``.sherry/`` (index + shadow-git + attachments), the CURRENT session's ENTIRE
  private tree (``plans/`` / ``attachments/`` / ``uploads/`` / ``evicted/`` and
  scratch files), ``TEMP_DIR``, ``STATIC_DIR`` and the uploaded-media areas.
* **read** — "would reading it leak a credential, private memory, or another
  session's transcript". Broader than write on purpose: the SOURCE TREE is
  readable (reading source neither breaks the runtime nor leaks a secret, and an
  agent debugging itself legitimately reads it) while it is not writable. The
  CURRENT session's whole private tree is readable too.

A path counts as "sherry's own" when it resolves inside ``ROOT_DIR`` or
``APP_HOME``. Anything inside a DIFFERENT session project directory is the
user's workspace: it never reaches this module's deny branch (the existing
in-root pass and external-path approval handle it).

Deliberately NOT routed through this module: every internal write path — the
``memory`` tool, ``skill_manage``, ``exit_plan_mode``, the evidence ledger,
shadow-git, the checkpointer, trajectory, heartbeat, the knowledge index. They
build their paths from ``config.path`` constants and never call
``resolve_tool_path`` / ``resolve_external_path``, so the whitelists cannot
interfere with the agent's own features.
"""

from __future__ import annotations

import os
from typing import Literal

from pathlib import Path

import config.path as paths

__all__ = [
    "SelfPathDecision",
    "classify_self_path",
    "is_own_session_path",
    "is_self_path",
    "is_sensitive_root_file",
    "own_session_dir",
    "read_denied_reason",
    "write_denied_reason",
]

SelfPathMode = Literal["read", "write"]
SelfPathDecision = Literal["allow", "deny"]

#: Files at the root of ``ROOT_DIR`` / ``APP_HOME`` that are configuration or
#: credentials: unreadable through the general file tools (``.env`` is tracked
#: application config; ``.models`` / ``.network`` carry keys for real).
_ROOT_SENSITIVE_FILES: tuple[str, ...] = (
    ".env",
    ".models",
    ".network",
    ".models.example",
    ".network.example",
    "sherry.jsonc",
)

#: Persona files at the workspace root. The client's panels own them; the agent
#: writes them through the ``memory`` tool (which has its own injection scan),
#: so a raw ``write_file`` on them is refused.
_WORKSPACE_PERSONA_FILES: tuple[str, ...] = (
    "AGENTS.md",
    "SOUL.md",
    "USER.md",
    "ROLE.md",
    "IDENTITY.md",
)

#: Model WEIGHT suffixes. Weights are unreadable on purpose (no legitimate use
#: through a text tool), but the ``models/`` DIRECTORY also holds product source
#: in a checkout (``models/LLMs/main_llm.py``), which must stay readable — so the
#: read denial matches by suffix rather than by directory.
_WEIGHT_SUFFIXES: tuple[str, ...] = (
    ".gguf",
    ".bin",
    ".safetensors",
    ".onnx",
    ".pt",
    ".pth",
    ".ckpt",
)


def _resolved(value: Path) -> Path:
    """Best-effort resolve (a non-existent path still yields an absolute one)."""
    try:
        return value.resolve()
    except (OSError, RuntimeError):
        return value


def _under(target: Path, root: Path) -> bool:
    """True when target is root or beneath it (both resolved)."""
    root = _resolved(root)
    return target == root or target.is_relative_to(root)


def _self_roots() -> tuple[Path, ...]:
    """The trees that count as sherry's own files (``ROOT_DIR`` and ``APP_HOME``)."""
    roots = [_resolved(paths.ROOT_DIR)]
    home = _resolved(paths.APP_HOME)
    if home not in roots:
        roots.append(home)
    return tuple(roots)


def is_self_path(resolved: Path) -> bool:
    """Whether *resolved* names a file of sherry's own (not the user's project)."""
    target = _resolved(resolved)
    return any(_under(target, root) for root in _self_roots())


def own_session_dir(session_id: str) -> Path | None:
    """This session's private ``SESSIONS_DIR/<session_id>`` tree, or ``None``.

    The single accessor for every own-session exemption (file-tool policy,
    external-path bypass, sandbox write surface). An empty or path-traversal
    session id answers ``None`` so callers fail closed.
    """
    session = (session_id or "").strip()
    if not session or not paths.is_safe_session_segment(session):
        return None
    return paths.SESSIONS_DIR / session


def is_own_session_path(resolved: Path, session_id: str) -> bool:
    """Whether *resolved* is inside THIS session's private ``SESSIONS_DIR`` tree.

    The owning session (main or child) may read/write every file under its own
    directory without HITL: plans, attachments, uploads, ``evicted/`` and the
    rest are the agent's private working state. A path under another session's
    directory is NOT covered — cross-session isolation stays intact.
    """
    directory = own_session_dir(session_id)
    return directory is not None and _under(_resolved(resolved), _resolved(directory))


def _write_allow_roots(session_id: str) -> tuple[Path, ...]:
    """Directories the general file tools may write to inside sherry's trees."""
    session = (session_id or "").strip()
    roots = [
        # The project's own .sherry/ (code index, shadow-git snapshots): these
        # are the agent's documented write surface inside a project.
        Path(paths.ROOT_DIR) / ".sherry",
        paths.TEMP_DIR,
        paths.STATIC_DIR,
        paths.SRC_DIR / "images",
        paths.SRC_DIR / "audio",
        paths.SRC_DIR / "video",
        paths.SRC_DIR / "rag" / "uploads",
    ]
    if session and paths.is_safe_session_segment(session):
        # The whole private session tree, not just plans/attachments/uploads:
        # the agent may also rewrite its own evicted previews and scratch files.
        roots.append(paths.SESSIONS_DIR / session)
    return tuple(_resolved(root) for root in roots)


def is_sensitive_root_file(path: Path) -> bool:
    """True for sherry's own root config/credential files (any of its trees).

    ``normcase`` matters on Windows: NTFS opens ``.ENV`` and ``.env`` as the same
    file, but a case-sensitive string compare would let the upper-case spelling
    through the deny list.
    """
    target = os.path.normcase(str(_resolved(path)))
    return any(
        target == os.path.normcase(str(root / name))
        for root in _self_roots()
        for name in _ROOT_SENSITIVE_FILES
    )


def _read_allowed_except_denied(path: Path, session_id: str) -> bool:
    """Whether a READ of *path* is refused.

    Reads are allowed across the source tree; the denials are the credential,
    memory and cross-session surfaces.
    """
    target = _resolved(path)

    # Configuration / credentials at the root of either tree.
    if is_sensitive_root_file(target):
        return True

    # Persona files and the memory / knowledge trees at the workspace root.
    workspace = _resolved(paths.WORKSPACE_DIR)
    for name in _WORKSPACE_PERSONA_FILES:
        if target == workspace / name:
            return True
    if _under(target, workspace / "memory") or _under(target, workspace / "knowledge"):
        return True

    # Runtime state and persistent stores.
    if (
        _under(target, paths.SRC_DIR / "data")
        or _under(target, paths.SRC_DIR / "checkpoints")
        or _under(target, paths.CODE_INTEL_DIR)
        or _under(target, paths.LOG_DIR)
        or _under(target, paths.MCP_SERVERS_DIR)
        or _under(target, paths.USER_SKILLS_DIR)
    ):
        return True

    # Model weights (the models/ DIRECTORY stays readable — it holds source too).
    if target.suffix.lower() in _WEIGHT_SUFFIXES:
        return True

    # Sessions: the CURRENT session's whole private tree is readable (plans,
    # attachments, uploads, evicted previews, scratch files). Every other
    # session (and the shared trajectory ledger) is another conversation.
    sessions = _resolved(paths.SESSIONS_DIR)
    if _under(target, sessions):
        return not is_own_session_path(target, session_id)

    return False


def read_denied_reason(resolved: Path, session_id: str = "") -> str | None:
    """The refusal text for a read of sherry's own files, or None when allowed.

    Returns None for paths that are NOT sherry's own (the caller's existing
    project/external flow keeps them), and for reads inside the read-allowed
    surface.
    """
    if not is_self_path(resolved):
        return None
    if not _read_allowed_except_denied(resolved, session_id):
        return None
    return (
        f"{resolved} is sherry's own configuration, memory or another session's "
        "state — reading it through a file tool is blocked"
    )


def write_denied_reason(resolved: Path, session_id: str = "") -> str | None:
    """The refusal text for a write to sherry's own files, or None when allowed."""
    if not is_self_path(resolved):
        return None
    target = _resolved(resolved)
    if any(_under(target, root) for root in _write_allow_roots(session_id)):
        return None
    return (
        f"{resolved} is sherry's own source, configuration or runtime state — "
        "the general file tools may only write .sherry/, this session's private "
        "directory, temp, static and the upload areas"
    )


def classify_self_path(
    resolved: Path, session_id: str = "", mode: SelfPathMode = "read"
) -> str | None:
    """One decision point for both modes: None = allowed, else the reason."""
    return (
        read_denied_reason(resolved, session_id)
        if mode == "read"
        else write_denied_reason(resolved, session_id)
    )
