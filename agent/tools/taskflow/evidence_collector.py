"""Collect a verification-evidence summary for judge prompts.

The ledger is append-only, so staleness is DERIVED, not stored: an evidence
row is stale iff a later ``{"event": "stale"}`` row in the same session scope
names a ``file_path`` contained in the row's ``command``.
"""

from __future__ import annotations

from collections.abc import Iterable

from agent.tools.pub_base.evidence_ledger import EvidenceLedger


def _is_stale(index: int, row: dict, stale_events: list[tuple[int, dict]]) -> bool:
    """True when a later stale event's path appears in this row's command."""
    command = str(row.get("command") or "")
    return any(
        stale_index > index and str(event.get("file_path") or "") in command
        for stale_index, event in stale_events
    )


def collect_evidence_summary(
    session_key: str | None = None,
    session_keys: Iterable[str] | None = None,
) -> str | None:
    """Build a human-readable evidence summary for the judge prompt.

    Scope by ONE key (``session_key`` — ``taskflow_resume`` passes the step's
    ``child_session_key``, the completion drain passes the session) or by
    several (``session_keys`` — ``taskflow_finish`` aggregates the flow's own
    scope: every dispatched step's child session plus the flow's creator,
    because evidence rows are keyed by session and a flow spans sessions).

    Rows are grouped per session in ONE pass over the ledger and each group
    keeps its own order: the staleness derivation compares a row with LATER
    stale events of the SAME session, so groups are rendered side by side,
    never interleaved. Returns None when no key has any evidence row.
    """
    keys = [key for key in [session_key, *(session_keys or [])] if str(key or "").strip()]
    ordered = list(dict.fromkeys(str(key) for key in keys))
    if not ordered:
        return None
    grouped = EvidenceLedger.read_for_keys(ordered)

    lines: list[str] = []
    for key in ordered:
        records = grouped.get(key)
        if not records:
            continue
        evidence_rows = [(i, r) for i, r in enumerate(records) if r.get("event") != "stale"]
        stale_events = [(i, r) for i, r in enumerate(records) if r.get("event") == "stale"]
        for index, row in evidence_rows:
            status = row.get("status", "unknown")
            icon = "pass" if status == "passed" else "FAIL" if status == "failed" else "?"
            stale_tag = " [stale]" if _is_stale(index, row, stale_events) else ""
            lines.append(
                f"  [{icon}] {row.get('kind', 'unknown')}: `{row.get('command', '')}`{stale_tag}"
            )
    return "\n".join(lines) or None
