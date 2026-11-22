"""The approved plan's INTENT anchor — the one place execution drift is measured against.

Why this exists
---------------
The todo list is the agent's working plan, but a todo row is a *task*, not an
*intent*: "patch step_judge.py" survives even when the user's goal was "make
judge outage fail closed". The original goal/scope lived only as free text in
``workspace/sessions/<id>/plans/plan-<revision>.md``, which the model had to
re-read on demand — drift was therefore invisible to both the per-turn context
and the verify gate.

This module makes the intent a first-class, SYSTEM-OWNED session value:

* it is parsed ONCE from the approved plan's ``## Goal`` / ``## Scope`` /
  ``accept:`` items when the plan is submitted (see ``exit_plan_mode``);
* it is dual-written into the in-memory + SQLite state registers under
  ``StateKey.PLAN_INTENT_ANCHOR`` (the same durability pattern as plan mode),
  and is only rewritten by a new plan revision — the model cannot edit it via
  ``todowrite`` (that tool has no ``metadata`` field);
* it is rendered into the per-turn ``todo_block`` reminder
  (``workspace.prompt_builder._build_todo_block``), so the intent rides INTO
  the context together with the todo list — including after a compression;
* it is handed to the reviewer as part of the verify brief and checked again
  by ``plan_drifted()`` before the verify todo may complete.

The plan file remains the full-detail source of truth; this anchor is its
compact, hash-bound view. ``revision`` IS ``sha256(plan)[:16]`` — if the file
changes without a replan, the hash no longer matches and the gate blocks.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from loguru import logger

from runtime import StateKey, state_register_db, state_register_mem
from workspace.intent_anchor_view import (
    format_intent_anchor,
    read_intent_anchor as _read_anchor_view,
)

__all__ = [
    "clear_intent_anchor",
    "format_intent_anchor",
    "parse_intent_anchor",
    "plan_drifted",
    "read_intent_anchor",
    "revision_of",
    "write_intent_anchor",
]

_HEADING_PREFIX = "## "
_MAX_GOAL_CHARS = 300
_MAX_ITEM_CHARS = 200
_MAX_ITEMS = 8


def revision_of(plan: str) -> str:
    """The approval revision token: sha256 of the plan text, first 16 hex chars."""
    return hashlib.sha256(plan.encode("utf-8")).hexdigest()[:16]


def _section(plan: str, name: str) -> str:
    """Return the body of the ``## <name>`` section ('' when absent)."""
    lines = plan.splitlines()
    wanted = name.strip().lower()
    collecting = False
    body: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(_HEADING_PREFIX):
            heading = stripped[len(_HEADING_PREFIX) :].strip().lower()
            if collecting:
                break
            collecting = heading == wanted
            continue
        if collecting:
            body.append(stripped)
    return "\n".join(body).strip()


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    if not flat:
        return ""
    return flat[:limit] + ("..." if len(flat) > limit else "")


def _scope_items(scope: str, labels: tuple[str, ...]) -> list[str]:
    """Pull ``- <label>: <text>`` (or a bare ``- <label>``) out of a Scope body."""
    items: list[str] = []
    for line in scope.splitlines():
        stripped = line.strip().lstrip("-*•").strip()
        if not stripped:
            continue
        for label in labels:
            low = stripped.lower()
            label_low = label.lower()
            if low.startswith(label_low):
                value = stripped[len(label) :].lstrip(" :：").strip()
                if value:
                    items.append(_one_line(value, _MAX_ITEM_CHARS))
                break
    return items[:_MAX_ITEMS]


def _acceptance_items(plan: str) -> list[str]:
    """Collect explicit ``accept:`` criteria plus the Verification section."""
    items: list[str] = []
    for line in plan.splitlines():
        low = line.lower()
        if "accept:" in low:
            value = line[low.index("accept:") + len("accept:") :].strip(" -—\t")
            value = _one_line(value, _MAX_ITEM_CHARS)
            if value:
                items.append(value)
    verification = _one_line(_section(plan, "Verification"), 240)
    if verification:
        items.append(f"overall: {verification}")
    return items[:_MAX_ITEMS]


def parse_intent_anchor(plan: str, *, revision: str, source_ref: str = "") -> dict[str, Any]:
    """Parse a submitted plan into a compact, immutable intent record.

    Fail-soft: a malformed/missing section simply yields an empty field, so a
    plan that does not follow the template still gets an anchor (with fewer
    fields) rather than blocking approval.
    """
    goal = _one_line(_section(plan, "Goal"), _MAX_GOAL_CHARS)
    scope = _section(plan, "Scope")
    must_have = _scope_items(scope, ("Must-have", "Must have", "必须"))
    must_not = _scope_items(
        scope,
        ("Must-NOT-have", "Must not have", "Must-NOT", "Must not", "禁止", "不得"),
    )
    return {
        "goal": goal,
        "must_have": must_have,
        "must_not_have": must_not,
        "acceptance": _acceptance_items(plan),
        "revision": revision,
        "source_ref": source_ref,
    }


def write_intent_anchor(session_id: str, anchor: dict[str, Any]) -> None:
    """Dual-write the anchor (mem for hot reads, DB for restart survival)."""
    if not session_id or not isinstance(anchor, dict) or not anchor:
        return
    for register in (state_register_mem, state_register_db):
        try:
            register.set_state(session_id, StateKey.PLAN_INTENT_ANCHOR, anchor)
        except Exception:  # noqa: BLE001 - state writes never break a tool
            logger.debug("intent anchor: {} write failed", register, exc_info=True)


def read_intent_anchor(session_id: str) -> dict[str, Any] | None:
    """Read the anchor through THIS module's register bindings (mem → DB)."""
    return _read_anchor_view(session_id, registers=(state_register_mem, state_register_db))


def clear_intent_anchor(session_id: str) -> None:
    """Drop the anchor (plan rejected / reset). Absent anchors are fine."""
    if not session_id:
        return
    for register in (state_register_mem, state_register_db):
        try:
            register.delete_state(session_id, StateKey.PLAN_INTENT_ANCHOR)
        except Exception:  # noqa: BLE001 - cleanup is best-effort
            logger.debug("intent anchor: {} clear failed", register, exc_info=True)


def plan_drifted(session_id: str) -> bool:
    """True when the approved plan file no longer hashes to the anchored revision.

    Legacy sessions with no anchor fail OPEN (no gate) — they predate this
    mechanism. A missing/unreadable file is also not treated as drift: the
    reviewer still checks the intent, but a deleted plan file must not strand
    an otherwise verifiable todo.
    """
    anchor = read_intent_anchor(session_id)
    if not anchor:
        return False
    revision = str(anchor.get("revision") or "")
    source_ref = str(anchor.get("source_ref") or "")
    if not revision or not source_ref:
        return False
    try:
        text = Path(source_ref).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return bool(text) and revision_of(text) != revision
