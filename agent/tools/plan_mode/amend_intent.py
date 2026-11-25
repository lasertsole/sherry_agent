"""``amend_intent`` — correct a misjudged intent WITHOUT a full replan.

Policy (explicitly chosen): the model MAY revise the approved intent anchor when
it notices its earlier interpretation of the user's request was wrong. The
revision takes effect immediately — the next todo reminder carries the new
intent and verify checks against it — and is recorded in the anchor's
``amendments`` audit list. There is deliberately NO mandatory user confirmation
and NO mandatory reviewer re-check of the amendment: adding either would make
the correction path as heavy as a replan, which is exactly what this tool
replaces. ``enter_plan_mode(replan=True)`` remains the right path when the
*scope* changed rather than the model's understanding of the intent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, override

from langchain_core.tools import BaseTool
from loguru import logger
from pydantic import BaseModel, Field

from agent.tools.plan_mode.intent_anchor import read_intent_anchor, write_intent_anchor
from agent.tools.pub_base import SessionId
from config.features import PLAN_MODE

__all__ = ["AmendIntentTool", "build_amend_intent_tool"]

_MAX_GOAL = 300
_MAX_REASON = 500
_MAX_ITEMS = 8
_MAX_ITEM = 200
_MAX_AMENDMENTS = 20
_AUDIT_KEYS = ("must_have", "must_not_have", "acceptance")


def _clean(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat[:limit] + ("..." if len(flat) > limit else "")


def _clean_list(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    return [_clean(value, _MAX_ITEM) for value in values[:_MAX_ITEMS] if str(value).strip()]


class AmendIntentInput(BaseModel):
    """Schema for the amend_intent tool arguments."""

    goal: str = Field(
        description="The corrected one-line statement of what the user actually wants.",
    )
    reason: str = Field(
        description=(
            "Why the earlier intent was wrong (audit record). Be concrete: name "
            "the evidence that changed your understanding."
        ),
    )
    must_have: list[str] | None = Field(
        default=None,
        description="Replace the Must-have items; omit to keep the current ones.",
    )
    must_not_have: list[str] | None = Field(
        default=None,
        description="Replace the Must-NOT-have items; omit to keep the current ones.",
    )
    acceptance: list[str] | None = Field(
        default=None,
        description="Replace the acceptance criteria; omit to keep the current ones.",
    )
    session_id: SessionId = ""


class AmendIntentTool(BaseTool):
    name: str = "amend_intent"
    description: str = (
        "Correct the approved plan's INTENT anchor when you discover your earlier "
        "interpretation of the request was wrong. The corrected intent immediately "
        "replaces the one shown with your todo list, and the change is recorded in "
        "an audit trail. No user approval or reviewer re-check is required. Use "
        "this ONLY for a genuinely misjudged intent: if the work SCOPE changed "
        "instead, call enter_plan_mode(replan=True)."
    )
    args_schema: type[BaseModel] = AmendIntentInput
    metadata: dict = {
        "idempotent": False,
        "side_effect_scope": "session",
    }

    def _amend(
        self,
        goal: str,
        reason: str,
        must_have: list[str] | None,
        must_not_have: list[str] | None,
        acceptance: list[str] | None,
        session_id: str,
    ) -> str:
        if not PLAN_MODE["enabled"]:
            return "Plan mode is disabled on this deployment; there is no intent anchor to amend."
        anchor = read_intent_anchor(session_id)
        if not anchor:
            return (
                "No approved intent anchor exists for this session, so there is "
                "nothing to amend. The anchor is created when a plan is approved."
            )
        new_goal = _clean(goal, _MAX_GOAL)
        audit_reason = _clean(reason, _MAX_REASON)
        if not new_goal:
            return "The corrected goal is empty. State what the user actually wants."
        if not audit_reason:
            return "A reason is required: the amendment is audited. Say what changed your mind."

        updated: dict[str, Any] = dict(anchor)
        old_goal = str(anchor.get("goal") or "")
        updated["goal"] = new_goal
        for key, value in (
            ("must_have", must_have),
            ("must_not_have", must_not_have),
            ("acceptance", acceptance),
        ):
            cleaned = _clean_list(value)
            if cleaned is not None:
                updated[key] = cleaned

        amendments = [item for item in (anchor.get("amendments") or []) if isinstance(item, dict)]
        amendments.append(
            {
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                "actor": "model",
                "reason": audit_reason,
                "from_goal": old_goal,
                "to_goal": new_goal,
            }
        )
        updated["amendments"] = amendments[-_MAX_AMENDMENTS:]
        # revision + source_ref are intentionally preserved: the plan file did
        # not change, so the verify drift gate must still consider it aligned.
        write_intent_anchor(session_id, updated)
        logger.info(
            "amend_intent: session {} goal amended ({} chars of reason)", session_id, len(reason)
        )
        return (
            f"Intent anchor amended. Goal is now: {new_goal}\n"
            "The correction is recorded in the anchor's audit trail and will ride "
            "with your todo list from the next turn; verify now checks against this "
            "corrected intent. Confirm it still matches the user's original request "
            "— this correction was not re-approved by a human."
        )

    @override
    def _run(
        self,
        goal: str,
        reason: str,
        must_have: list[str] | None = None,
        must_not_have: list[str] | None = None,
        acceptance: list[str] | None = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> str:
        return self._amend(goal, reason, must_have, must_not_have, acceptance, session_id)

    @override
    async def _arun(
        self,
        goal: str,
        reason: str,
        must_have: list[str] | None = None,
        must_not_have: list[str] | None = None,
        acceptance: list[str] | None = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> str:
        return self._amend(goal, reason, must_have, must_not_have, acceptance, session_id)


def build_amend_intent_tool() -> AmendIntentTool:
    tool: AmendIntentTool = AmendIntentTool()
    tool.handle_tool_error = True
    return tool
