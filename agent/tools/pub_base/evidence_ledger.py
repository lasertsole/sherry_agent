"""Append-only evidence ledger (``src/data/evidence-ledger.jsonl``).

One JSON object per line records the VERIFICATION trail: a recognized test /
lint / build / typecheck / format command the agent ran (``terminal`` and
``python_repl`` auto-record it), a file edit that invalidates earlier
verification (``{"event": "stale", "file_path": …}``), or a workspace merge
that invalidates the paths it landed. Appends never rewrite existing lines: a
locked ``"a"`` append of one line is the whole storage contract.

Rows are keyed by SESSION (``session_id`` — the main session's id, or a child's
``agent:{agent}:subagent:{uuid}`` key); there is no plan / todo / flow key.
The file stays repo-scoped and shared, so a per-session view filters it and a
flow-level view aggregates the session keys of its steps.

``LEDGER_PATH`` is a class attribute so tests can redirect it into ``tmp_path``.
"""

import json
import os
from collections.abc import Iterable
from datetime import datetime, UTC
from pathlib import Path

from loguru import logger

from agent.tools.pub_base import file_write_lock
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
        touched. Missing parent directories are created. The append takes the
        shared per-path write lock (in-process lock + ``flock``), so two
        processes appending at once cannot interleave a half-written line.
        """
        record = dict(entry)
        record["timestamp"] = datetime.now(UTC).isoformat()
        parent = os.path.dirname(os.path.abspath(cls.LEDGER_PATH))
        os.makedirs(parent, exist_ok=True)
        with file_write_lock(Path(cls.LEDGER_PATH)):
            with open(cls.LEDGER_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    @classmethod
    def read_all(cls) -> list[dict]:
        """Read every entry back; a missing file is an empty ledger.

        A line that does not parse (a torn write from a pre-lock build, a
        hand-edited file) is SKIPPED with one warning naming the line numbers
        and snippets: a single bad line must never make the whole ledger
        unreadable, because every consumer is a fail-open gate that would
        otherwise silently degrade to "pass".
        """
        if not os.path.exists(cls.LEDGER_PATH):
            return []
        records: list[dict] = []
        broken: list[str] = []
        with open(cls.LEDGER_PATH, encoding="utf-8") as f:
            for number, line in enumerate(f, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    records.append(json.loads(text))
                except json.JSONDecodeError:
                    broken.append(f"line {number}: {text[:80]}")
        if broken:
            logger.warning(
                "evidence ledger: skipped {} unparsable line(s) [{}]",
                len(broken),
                "; ".join(broken[:3]),
            )
        return records

    @classmethod
    def read_for_keys(cls, session_keys: Iterable[str]) -> dict[str, list[dict]]:
        """ONE pass over the file, grouped by the requested session keys.

        Grouping preserves the file's order WITHIN each key: the staleness
        derivation ("a later stale event") is only meaningful inside one
        session's row sequence. Keys with no rows are absent from the result
        and blank keys are ignored.
        """
        wanted = {str(key).strip() for key in session_keys if str(key or "").strip()}
        if not wanted:
            return {}
        grouped: dict[str, list[dict]] = {}
        for row in cls.read_all():
            key = str(row.get("session_id") or "")
            if key in wanted:
                grouped.setdefault(key, []).append(row)
        return grouped

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
