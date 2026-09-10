"""Append-only evidence ledger (``.omo/ledger.jsonl``).

One JSON object per line records the evidence trail for a plan's checkboxes
(``task-started`` / ``task-completed`` ...). Appends never rewrite existing
lines: opening in ``"a"`` mode and writing one line at a time is the whole
storage contract.

``LEDGER_PATH`` is a class attribute so tests can redirect it into ``tmp_path``.
"""

import json
import os
from datetime import datetime, UTC


class EvidenceLedger:
    """Class-level append/read API over a JSONL evidence file."""

    LEDGER_PATH = ".omo/ledger.jsonl"

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


__all__ = ["EvidenceLedger"]
