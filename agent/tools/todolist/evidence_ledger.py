"""Append-only evidence ledger (``src/data/evidence-ledger.jsonl``).

One JSON object per line records the evidence trail for a plan's checkboxes
(``task-started`` / ``task-completed`` ...). Appends never rewrite existing
lines: opening in ``"a"`` mode and writing one line at a time is the whole
storage contract.

``LEDGER_PATH`` is a class attribute so tests can redirect it into ``tmp_path``.
"""

import json
import os
from datetime import datetime, UTC

from config.path import resolve_evidence_ledger_path


class EvidenceLedger:
    """Class-level append/read API over a JSONL evidence file."""

    # Repo absolute (never cwd-relative). Deliberately NOT session-scoped:
    # this ledger is the shared audit trail across every plan and session.
    LEDGER_PATH = str(resolve_evidence_ledger_path())

    @classmethod
    def append(cls, entry: dict) -> None:
        """Append one entry as a single JSON line, stamping a UTC timestamp.

        The caller's dict is copied, not mutated, and existing lines are never
        touched. Missing parent directories are created.
        """
        record = dict(entry)
        record["timestamp"] = datetime.now(UTC).isoformat()
        parent = os.path.dirname(os.path.abspath(cls.LEDGER_PATH))
        os.makedirs(parent, exist_ok=True)
        with open(cls.LEDGER_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    @classmethod
    def read_all(cls) -> list[dict]:
        """Read every entry back; a missing file is an empty ledger."""
        if not os.path.exists(cls.LEDGER_PATH):
            return []
        with open(cls.LEDGER_PATH, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    # ── Session-scoped views ────────────────────────────────────────────────

    @classmethod
    def for_session(cls, session_key: str) -> "SessionEvidenceLedger":
        """Return a session-scoped view over the shared JSONL ledger.

        The underlying file stays shared; reads filter by ``session_id`` and
        writes inject it.
        """
        return SessionEvidenceLedger(session_key)

    @classmethod
    def read_for_session(cls, session_key: str) -> list[dict]:
        """Read every entry whose ``session_id`` equals ``session_key``."""
        return [r for r in cls.read_all() if r.get("session_id") == session_key]

    @classmethod
    def mark_stale_for_path(cls, file_path: str, session_key: str | None = None) -> int:
        """Append a ``stale`` event for ``file_path`` (append-only).

        No historical line is rewritten (the module contract is append-only).
        The read side derives staleness: an evidence row is stale iff a LATER
        ``{"event": "stale"}`` row names a ``file_path`` contained in its
        ``command``. Returns the count of currently-valid evidence rows that
        touch ``file_path`` — for logging only.
        """
        count = 0
        for record in cls.read_all():
            if record.get("event") == "stale":
                continue
            if session_key and record.get("session_id") != session_key:
                continue
            if file_path in str(record.get("command") or ""):
                count += 1
        cls.append({"event": "stale", "file_path": file_path, "session_id": session_key or ""})
        return count


class SessionEvidenceLedger:
    """Session-scoped view over :class:`EvidenceLedger` (one shared file)."""

    def __init__(self, session_key: str):
        self.session_key = session_key

    def append(self, **entry_fields) -> None:
        """Append an evidence row, stamping this view's ``session_id``."""
        entry_fields["session_id"] = self.session_key
        EvidenceLedger.append(entry_fields)

    def list_records(self) -> list[dict]:
        """List this session's evidence rows."""
        return EvidenceLedger.read_for_session(self.session_key)

    def mark_stale_for_path(self, file_path: str) -> int:
        """Append a stale event for ``file_path`` scoped to this session."""
        return EvidenceLedger.mark_stale_for_path(file_path, self.session_key)


__all__ = ["EvidenceLedger", "SessionEvidenceLedger"]
