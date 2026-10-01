"""Per-session project directory: the working root every tool resolves against.

The value lives in the session state register (``StateKey.PROJECT_DIR``), written
by ``PUT /sessions/project`` and read on the hot path by the tools, the path
guard and the prompt builder. Because those readers live in different packages —
``agent/**``, ``workspace/**`` and ``server/**`` — this module is the shared leaf:
it may only depend on ``config`` and ``runtime`` (import-linter keeps
``workspace/**`` away from ``agent/**``, so the reader cannot live in the tool
layer).

Two read depths, deliberately split:

- :func:`read_project_dir` is mem-only and safe to call synchronously inside a
  tool call (the register's mem tier is the agent-side fast path).
- :func:`read_project_dir_durable` falls back to the SQLite mirror and
  rehydrates mem. The SQLite read is blocking, so async callers (the HTTP layer)
  must wrap it in ``asyncio.to_thread`` — the same contract as
  ``session_settings_service._read_value``.

The process default (:func:`config.path.resolve_default_project_dir`) is only
consulted when the session has no binding; a bound value is always the session's
own, so two sessions can never see each other's directory.
"""

from __future__ import annotations

from pathlib import Path

from config.path import resolve_default_project_dir
from runtime.session.state_keys import StateKey

__all__ = [
    "ProjectDirSource",
    "current_project_dir",
    "project_dir_source",
    "read_project_dir",
    "read_project_dir_durable",
    "write_project_dir",
    "write_pending_project_dir",
]

#: Where the effective directory came from: an explicit session binding, the
#: process default (env / sherry.jsonc), or the repository root (nothing set).
ProjectDirSource = str  # Literal["session", "env", "default"] — kept as str for callers

_SESSION = "session"
_DEFAULT = "default"


def read_project_dir(session_id: str | None) -> Path | None:
    """The session's bound project directory (mem tier only); ``None`` when unset.

    Mem-only on purpose: this runs inside tool calls on the event loop, and the
    durable tier is a blocking SQLite read. The HTTP layer rehydrates mem via
    :func:`read_project_dir_durable` before the agent runs.
    """
    if not session_id:
        return None
    from runtime.session.state_register import state_register_mem

    raw = state_register_mem.get_state(session_id, StateKey.PROJECT_DIR, None)
    return _as_dir(raw)


def read_project_dir_durable(session_id: str | None) -> Path | None:
    """Like :func:`read_project_dir`, but falls back to the SQLite mirror.

    A hit in the mirror rehydrates the mem tier so the agent-side readers see
    the persisted value without another database hit. Blocking — async callers
    must run it in a worker thread.
    """
    bound = read_project_dir(session_id)
    if bound is not None or not session_id:
        return bound
    from runtime import state_register_db
    from runtime.session.state_register import state_register_mem

    raw = state_register_db.get_state(session_id, StateKey.PROJECT_DIR, None)
    directory = _as_dir(raw)
    if directory is not None:
        state_register_mem.set_state(session_id, StateKey.PROJECT_DIR, str(directory))
    return directory


def current_project_dir(session_id: str | None) -> Path:
    """The directory the session's tools must resolve against — never ``None``.

    A session binding wins; otherwise the process default from
    :func:`config.path.resolve_default_project_dir` (env → sherry.jsonc →
    repository root). Use :func:`project_dir_source` when the caller must know
    whether that default was an explicit choice or a fallback.
    """
    return read_project_dir(session_id) or resolve_default_project_dir()


def project_dir_source(session_id: str | None) -> ProjectDirSource:
    """``"session"`` when the session is bound, else ``"env"`` or ``"default"``.

    ``"env"`` means the process default came from ``SHERRY_PROJECT_DIR`` or the
    ``sherry.jsonc`` ``project_dir`` key; ``"default"`` means nothing was
    configured and the repository root is in effect. Callers use this to render
    "unbound" states and to leave a trace when a session silently runs against
    the repository.
    """
    if read_project_dir(session_id) is not None:
        return _SESSION
    default = resolve_default_project_dir()
    from config import ROOT_DIR

    return _DEFAULT if default == ROOT_DIR else "env"


def write_project_dir(session_id: str, directory: Path | str, *, pending: bool = False) -> None:
    """Persist the session's project directory (mem + durable mirror).

    ``pending=True`` writes the parked twin instead (the in-flight turn keeps
    the old root; the turn boundary promotes it) — see
    ``server.service.session_project_service``.
    """
    from runtime import state_register_db
    from runtime.session.state_register import state_register_mem

    key = StateKey.PROJECT_DIR_PENDING if pending else StateKey.PROJECT_DIR
    value = str(directory)
    state_register_mem.set_state(session_id, key, value)
    state_register_db.set_state(session_id, key, value)


def write_pending_project_dir(session_id: str, directory: Path | str | None) -> None:
    """Write (or clear, with ``None``) the parked directory choice."""
    from runtime import state_register_db
    from runtime.session.state_register import state_register_mem

    if directory is None:
        state_register_mem.delete_state(session_id, StateKey.PROJECT_DIR_PENDING)
        state_register_db.delete_state(session_id, StateKey.PROJECT_DIR_PENDING)
        return
    value = str(directory)
    state_register_mem.set_state(session_id, StateKey.PROJECT_DIR_PENDING, value)
    state_register_db.set_state(session_id, StateKey.PROJECT_DIR_PENDING, value)


def _as_dir(raw: object) -> Path | None:
    """Coerce a stored value to a Path; anything non-string/blank -> ``None``."""
    if isinstance(raw, Path):
        return raw
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip())
    return None
