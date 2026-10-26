"""Collect a verification-evidence summary for judge prompts.

The ledger is append-only, so staleness is DERIVED, not stored: an evidence
row is stale iff a later stale event in the same session scope names a
``file_path`` contained in the row's ``command``. A later ``broken`` event (a
finish-time re-verification that now fails) is the stronger signal and renders
as ``[broken]``; an ``unmark`` event clears either flag for its command.
"""

from __future__ import annotations

from collections.abc import Iterable

from agent.tools.pub_base.evidence_ledger import EvidenceLedger, derive_evidence_state


def collect_evidence_summary(
    session_key: str | None = None,
    session_keys: Iterable[str] | None = None,
    flow_id: str | None = None,
) -> str | None:
    """Build a human-readable evidence summary for the judge prompt.

    Scope by ONE key (``session_key`` — ``taskflow_resume`` passes the step's
    ``child_session_key``, the completion drain passes the session), by several
    (``session_keys`` — ``taskflow_finish`` aggregates the flow's own scope:
    every dispatched step's child session plus the flow's creator), and/or by
    ``flow_id`` (the TaskFlow finish path: rows stamped with the flow are read
    directly; older rows in sessions without any flow-stamped row fall back to
    the session keys).

    Rows are grouped per session in ONE pass over the ledger and each group
    keeps its own order: the staleness derivation compares a row with LATER
    events of the SAME session, so groups are rendered side by side, never
    interleaved. Returns None when no key has any evidence row.
    """
    keys = [key for key in [session_key, *(session_keys or [])] if str(key or "").strip()]
    ordered = list(dict.fromkeys(str(key) for key in keys))
    flow_id = str(flow_id or "").strip()
    if not ordered and not flow_id:
        return None

    grouped = EvidenceLedger.read_for_keys(ordered)
    if flow_id:
        by_flow = EvidenceLedger.read_for_flow(flow_id)
        covered_sessions = {str(row.get("session_id") or "") for row in by_flow}
        legacy_keys = [key for key in ordered if key not in covered_sessions]
        grouped = EvidenceLedger.read_for_keys(legacy_keys) if legacy_keys else {}
        for row in by_flow:
            grouped.setdefault(str(row.get("session_id") or ""), []).append(row)
        ordered = list(
            dict.fromkeys([*ordered, *(str(row.get("session_id") or "") for row in by_flow)])
        )

    lines: list[str] = []
    for key in ordered:
        records = grouped.get(key)
        if not records:
            continue
        for index, row in enumerate(records):
            if row.get("event"):
                continue  # event rows (stale/broken/unmark) are not evidence
            status = row.get("status", "unknown")
            icon = "pass" if status == "passed" else "FAIL" if status == "failed" else "?"
            broken, stale = derive_evidence_state(records, index)
            tag = " [broken]" if broken else " [stale]" if stale else ""
            lines.append(
                f"  [{icon}] {row.get('kind', 'unknown')}: `{row.get('command', '')}`{tag}"
            )
    return "\n".join(lines) or None
