"""Neutral read/render helpers for the approved plan's intent anchor.

Lives in ``workspace`` (not ``agent``) so the live todo reminder can read it
without crossing the workspace→agent import boundary. The writer/parser lives
in ``agent.tools.plan_mode.intent_anchor``; both sides agree on the
``StateKey.PLAN_INTENT_ANCHOR`` payload shape.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from loguru import logger

from runtime import StateKey, state_register_db, state_register_mem

__all__ = ["format_intent_anchor", "read_intent_anchor"]


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    if not flat:
        return ""
    return flat[:limit] + ("..." if len(flat) > limit else "")


def read_intent_anchor(
    session_id: str, *, registers: Sequence[Any] | None = None
) -> dict[str, Any] | None:
    """Return the session's SYSTEM-owned intent anchor (mem first, DB fallback).

    ``registers`` lets the agent-side module pass its own register bindings, so
    tests can replace one tier without having to patch this module too.
    """
    if not session_id:
        return None
    tiers = registers if registers is not None else (state_register_mem, state_register_db)
    for register in tiers:
        try:
            value = register.get_state(session_id, StateKey.PLAN_INTENT_ANCHOR, None)
        except Exception:  # noqa: BLE001 - fail-open read
            logger.debug("intent anchor: {} read failed", register, exc_info=True)
            continue
        if isinstance(value, dict) and value:
            return value
    return None


def format_intent_anchor(anchor: dict[str, Any], *, max_chars: int = 560) -> str:
    """Render the compact per-turn reminder view of the anchor.

    Kept short on purpose: the ``todo_block`` reminder has a 1500-char budget
    that must also fit every todo row. The full plan stays on disk at
    ``source_ref`` for on-demand reading.
    """
    if not isinstance(anchor, dict) or not anchor:
        return ""
    lines = ["Intent Anchor (approved intent — do not drift):"]
    goal = _one_line(str(anchor.get("goal") or ""), 300)
    if goal:
        lines.append(f"  Goal: {goal}")
    for label, key in (("Must-have", "must_have"), ("Must-NOT-have", "must_not_have")):
        values = anchor.get(key)
        if isinstance(values, list) and values:
            joined = "; ".join(_one_line(str(v), 100) for v in values[:4])
            lines.append(f"  {label}: {joined}")
    acceptance = anchor.get("acceptance")
    if isinstance(acceptance, list) and acceptance:
        joined = "; ".join(_one_line(str(v), 90) for v in acceptance[:4])
        lines.append(f"  Accept: {joined}")
    lines.append(
        "  Work only within this intent; if it no longer fits, call "
        "enter_plan_mode(replan=True) — do not drift silently."
    )
    text = "\n".join(lines)
    if len(text) > max_chars:
        return text[: max(0, max_chars - 3)] + "..."
    return text
