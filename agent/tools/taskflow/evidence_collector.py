"""Collect a verification-evidence summary for judge prompts.

The ledger is append-only, so staleness is DERIVED, not stored: an evidence
row is stale iff a later ``{"event": "stale"}`` row in the same session scope
names a ``file_path`` contained in the row's ``command``.
"""

from __future__ import annotations

from agent.tools.todolist.evidence_ledger import EvidenceLedger


def _is_stale(index: int, row: dict, stale_events: list[tuple[int, dict]]) -> bool:
    """True when a later stale event's path appears in this row's command."""
    command = str(row.get("command") or "")
    return any(
        stale_index > index and str(event.get("file_path") or "") in command
        for stale_index, event in stale_events
    )


def collect_evidence_summary(
    session_key: str | None = None,
    flow_id: str | None = None,
) -> str | None:
    """Build a human-readable evidence summary for the judge prompt.

    Callers scope by exactly one key: ``taskflow_resume`` passes
    ``session_key`` (the step's child_session_key); ``taskflow_finish`` passes
    ``flow_id``. Returns None when no evidence was recorded.
    """
    records = EvidenceLedger.for_session(session_key or flow_id or "default").list_records()
    evidence_rows = [(i, r) for i, r in enumerate(records) if r.get("event") != "stale"]
    stale_events = [(i, r) for i, r in enumerate(records) if r.get("event") == "stale"]
    if not evidence_rows:
        return None

    lines: list[str] = []
    for index, row in evidence_rows:
        status = row.get("status", "unknown")
        icon = "pass" if status == "passed" else "FAIL" if status == "failed" else "?"
        stale_tag = " [stale]" if _is_stale(index, row, stale_events) else ""
        lines.append(
            f"  [{icon}] {row.get('kind', 'unknown')}: `{row.get('command', '')}`{stale_tag}"
        )
    return "\n".join(lines)
