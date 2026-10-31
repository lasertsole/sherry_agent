"""Duplicate-flow detection for TaskFlow create/plan.

Text-only and fast: a substring match or a high token overlap against an
already-active flow in the SAME session returns that flow. The caller returns a
warning and lets the model either reuse it or re-call with ``ack_dup=True``;
this is a hint, never a hard refusal.
"""

from __future__ import annotations

import re

from ..registry import store_sqlite

_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def normalize_duplicate_text(value: object) -> str:
    """Case-fold and collapse whitespace/punctuation for comparison."""
    text = str(value or "").strip().lower()
    return " ".join(_TOKEN_RE.findall(text))


def token_overlap(left: str, right: str) -> float:
    """Jaccard overlap of the normalized token sets (0.0 when either is empty)."""
    a = set(_TOKEN_RE.findall(left))
    b = set(_TOKEN_RE.findall(right))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _flow_description(flow: dict) -> str:
    state = flow.get("state") or {}
    return str(state.get("description") or flow.get("description") or "")


async def find_duplicate_flow(
    session_id: str, goal: str | None, description: str | None
) -> dict | None:
    """Return an active same-session flow whose goal looks like this one."""
    target = normalize_duplicate_text(goal or description)
    if not target:
        return None
    try:
        flows = await store_sqlite.get_non_terminal_flows(session_id)
    except Exception:
        return None
    for flow in flows:
        existing = normalize_duplicate_text(_flow_description(flow))
        if not existing:
            continue
        if target in existing or existing in target or token_overlap(target, existing) > 0.7:
            return flow
    return None


def duplicate_flow_message(flow: dict) -> str:
    """Model-facing hint with the reuse and explicit-ack escape hatches."""
    flow_id = str(flow.get("flow_id") or "")
    status = str(flow.get("status") or "unknown")
    goal = _flow_description(flow)[:200]
    return (
        f"Possible duplicate: flow '{flow_id}' (status={status}, goal={goal!r}) is "
        "already active in this session and looks similar. Reuse it "
        f"(taskflow_step(action='resume', flow_id='{flow_id}', ...)) or, if this is "
        "a genuinely different task, re-call with ack_dup=True."
    )


__all__ = [
    "duplicate_flow_message",
    "find_duplicate_flow",
    "normalize_duplicate_text",
    "token_overlap",
]
