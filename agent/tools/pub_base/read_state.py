"""Per-session file read licenses — the precondition ``write_file`` needs.

``write_file`` is a blind overwrite: it never reads the target, so it has no
revision to carry into ``atomic_write_text_no_follow``'s CAS and could silently
destroy content this session never saw (two agents, one write each, both
reporting success). ZCode's answer is to refuse a write to a file the session
has not read; the refusal needs a memory of what the session has read, per
file, per revision. This module is that memory:

* :func:`note_read` — a COMPLETE ``read_file``: the session holds the whole
  content, so overwriting that exact revision is licensed;
* :func:`note_overwrite` — this session wrote the whole file (``write_file``
  without ``append``), so it knows the revision it produced: the license is
  created, or moved forward;
* :func:`note_edit` — this session patched or appended in place: its knowledge
  moved by its own delta, so an EXISTING license advances and no license is
  invented for a file the session never read.

:func:`licensed_revision` returns the revision ``write_file`` may cite as the
atomic write's ``expected_revision`` (``None`` = no license: the write must be
refused). The value is a plain revision id, so a license that went stale is
refused by the same check that catches a concurrent writer.

The registry is process-local and deliberately not persisted: a restart forgets
the licenses, and the next overwrite of an existing file answers "read it
first" — one extra read, never a lost edit. It is capped LRU-style; eviction
drops protection, never grants it.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path

__all__ = [
    "forget_all",
    "forget_session",
    "licensed_revision",
    "note_edit",
    "note_overwrite",
    "note_read",
]

#: Upper bound on remembered licenses (one per session and resolved path). A
#: forgotten license only costs a re-read, so the cap can stay small; the LRU
#: order is what decides which one goes.
_MAX_LICENSES = 4096

#: ``{(session_id, resolved path): revision}`` in LRU order (oldest first).
_LICENSES: OrderedDict[tuple[str, str], str] = OrderedDict()
_LOCK = threading.Lock()


def _key(session_id: str, path: Path) -> tuple[str, str]:
    return (session_id, str(path))


def _store(session_id: str, path: Path, revision: str) -> None:
    key = _key(session_id, path)
    with _LOCK:
        _LICENSES[key] = revision
        _LICENSES.move_to_end(key)
        while len(_LICENSES) > _MAX_LICENSES:
            _LICENSES.popitem(last=False)


def note_read(session_id: str, path: Path, revision: str) -> None:
    """Record that *session_id* has read the whole of *path* at *revision*."""
    _store(session_id, path, revision)


def note_overwrite(session_id: str, path: Path, revision: str) -> None:
    """Record a whole-file write by *session_id*: it knows the new *revision*."""
    _store(session_id, path, revision)


def note_edit(session_id: str, path: Path, revision: str) -> None:
    """Advance an existing license after an in-place edit (append / patch).

    A session that never read the file keeps no license — its own delta must
    not invent one, or the next overwrite would be licensed off content the
    session never saw.
    """
    key = _key(session_id, path)
    with _LOCK:
        if key in _LICENSES:
            _LICENSES[key] = revision
            _LICENSES.move_to_end(key)


def licensed_revision(session_id: str, path: Path) -> str | None:
    """The revision *session_id* is licensed to overwrite, or ``None``."""
    key = _key(session_id, path)
    with _LOCK:
        revision = _LICENSES.get(key)
        if revision is not None:
            _LICENSES.move_to_end(key)
    return revision


def forget_session(session_id: str) -> None:
    """Drop every license of one session (session teardown, tests)."""
    with _LOCK:
        for key in [k for k in _LICENSES if k[0] == session_id]:
            del _LICENSES[key]


def forget_all() -> None:
    """Drop every license (tests; a restart has the same effect)."""
    with _LOCK:
        _LICENSES.clear()
