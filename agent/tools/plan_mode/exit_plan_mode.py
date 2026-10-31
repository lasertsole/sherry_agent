"""``exit_plan_mode`` — submit the plan and suspend the graph for approval.

The submitted plan is persisted under the session's plans directory
(``SESSIONS_DIR/<session_id>/plans/plan-<revision>.md``; the filename derives
from the plan's hash, so the re-execution that follows a resume writes the same
file), and the graph suspends with a HITL ``interrupt`` carrying the plan, so
the client renders it for the user.

Decisions (mirroring the ``question`` tool's contract):

* **approve** → status ``ready``; the plan stays the session's reference and the
  execution tools become available again.
* **reject WITH a message** → the message is the requested change: the status
  stays ``planning`` and the mutation counter increments (budget:
  ``PLAN_MODE["max_mutations"]`` reject-with-feedback rounds).
* **reject WITHOUT a message** → the plan is dropped: status ``needs_planning``.

The approval binds to a revision token (``sha256(plan)[:16]``): the token is
recomputed on the re-executed call and must equal what was stored before the
interrupt, so a plan that changed while it awaited approval cannot ride an old
approval.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, override

from langchain.agents.middleware.human_in_the_loop import (
    ActionRequest,
    HITLRequest,
    ReviewConfig,
)
from langchain_core.tools import BaseTool
from langgraph.types import interrupt
from loguru import logger
from pydantic import BaseModel, Field

from agent.tools.plan_mode.state import (
    PLAN_ACTIVE_STATUSES,
    PLAN_MODE,
    read_plan_field,
    read_plan_status,
    set_plan_state,
)
from agent.tools.pub_base import SessionId
from config.features import (
    PLAN_STATUS_IDLE,
    PLAN_STATUS_PLANNING,
    PLAN_STATUS_READY,
)
from runtime import StateKey

_PLAN_TEMPLATE = (
    "## Goal\n"
    "<one-line restatement of the user's request>\n\n"
    "## Scope\n"
    "- Must-have: <what the plan must achieve>\n"
    "- Must-NOT-have: <explicit boundaries>\n\n"
    "## Steps\n"
    "1. <step> — depends on: none — accept: <acceptance criterion>\n"
    "2. <step> — depends on: 1 — accept: <acceptance criterion>\n\n"
    "## Verification\n"
    "<how the whole thing gets verified: test command, type check, manual check>\n\n"
    "## Risk\n"
    "<main risks + mitigations>"
)


def _revision_of(plan: str) -> str:
    return hashlib.sha256(plan.encode("utf-8")).hexdigest()[:16]


class ExitPlanModeInput(BaseModel):
    """Schema for the exit_plan_mode tool arguments."""

    plan: str = Field(
        description=(
            "The complete plan as self-contained markdown, following this "
            f"template:\n\n{_PLAN_TEMPLATE}"
        ),
    )
    session_id: SessionId = ""


class ExitPlanModeTool(BaseTool):
    name: str = "exit_plan_mode"
    description: str = (
        "Submit the plan you designed in plan mode for the user's approval. "
        "The user can approve it, request changes (the feedback comes back and "
        "you revise and resubmit), or reject it. After approval plan mode exits "
        "and the write tools become available again: execute the approved plan.\n"
        "Call it ONLY while plan mode is active (after enter_plan_mode), and end "
        "the turn on this call — there is nothing else to do until the user "
        "decides."
    )
    args_schema: type[BaseModel] = ExitPlanModeInput
    # Not idempotent (it persists a plan and suspends), and the wait for the
    # user must not count against the heartbeat staleness watchdog.
    metadata: dict = {
        "idempotent": False,
        "skip_heartbeat": True,
        "nudge": True,
        "side_effect_scope": "user_interaction",
    }

    @staticmethod
    def _persist_plan(session_id: str, plan: str, revision: str) -> str:
        """Write the plan under the session's plans dir; return its path (or "")."""
        try:
            from config.path import session_plans_dir

            plans_dir = session_plans_dir(session_id)
            if plans_dir is None:
                return ""
            plans_dir.mkdir(parents=True, exist_ok=True)
            path = plans_dir / f"plan-{revision}.md"
            from agent.tools.pub_base.atomic_write import atomic_write_text_no_follow

            atomic_write_text_no_follow(path, plan)
            return str(path)
        except Exception:  # noqa: BLE001 - the approval must not depend on the write
            logger.warning("exit_plan_mode: plan file write failed", exc_info=True)
            return ""

    def _submit(self, plan: str, session_id: str) -> str:
        if not PLAN_MODE["enabled"]:
            return "Plan mode is disabled on this deployment; proceed directly."
        status = read_plan_status(session_id)
        if status not in PLAN_ACTIVE_STATUSES:
            return (
                "exit_plan_mode is only valid while plan mode is active — call "
                "enter_plan_mode first."
            )
        plan = plan.strip()
        if not plan:
            return "The plan is empty. Write the plan and submit it again."
        max_chars = int(PLAN_MODE["max_plan_chars"])
        if len(plan) > max_chars:
            return (
                f"The plan is {len(plan)} characters; the limit is {max_chars}. "
                "Tighten it (keep the goal, scope, steps with acceptance "
                "criteria, verification and risks) and submit again."
            )

        # Mutation budget: past N reject-with-feedback rounds the tool stops
        # re-submitting so a plan cannot be revised forever.
        mutations = int(read_plan_field(session_id, StateKey.PLAN_MUTATION_COUNT, 0) or 0)
        budget = int(PLAN_MODE["max_mutations"])
        if mutations >= budget:
            return (
                f"This plan has already been revised {mutations} times (budget "
                f"{budget}). Stop re-submitting: ask the user to approve it as "
                "it stands or reject it outright."
            )

        revision = _revision_of(plan)
        path = self._persist_plan(session_id, plan, revision)
        set_plan_state(
            session_id,
            revision=revision,
            file_path=path or None,
        )

        first_line = next(
            (line.strip("# ").strip() for line in plan.splitlines() if line.strip()), ""
        )
        hitl_request = HITLRequest(
            action_requests=[
                ActionRequest(
                    name=self.name,
                    args={"plan": plan, "revision": revision},
                    description=first_line or "Plan awaiting approval",
                )
            ],
            review_configs=[
                ReviewConfig(
                    action_name=self.name,
                    allowed_decisions=["approve", "reject"],
                )
            ],
        )
        # A headless turn has no approver: suspending would strand the graph in
        # plan mode (read-only) with nobody able to answer. Refuse and tell the
        # model to stop — retrying the same call cannot help.
        from agent.middlewares.humanInTheLoop.approval_scope import no_operator_in_scope

        if no_operator_in_scope():
            logger.info("[ExitPlanModeTool] headless turn — refusing instead of suspending")
            return (
                "Error: no human operator is present to approve a plan in this "
                "automated turn. Do not retry: stop here and report that the plan "
                "needs a human to approve it."
            )
        logger.debug("[ExitPlanModeTool] suspending graph for plan {}", revision)
        response = interrupt(hitl_request)

        # Revision binding — the approval belongs to the plan that was submitted.
        stored = read_plan_field(session_id, StateKey.PLAN_REVISION, "")
        if stored != revision:
            logger.warning(
                "exit_plan_mode: revision mismatch ({} != {}) — approval discarded",
                revision,
                stored,
            )
            return (
                "The plan changed while it awaited approval, so the approval was "
                "discarded. Re-submit the current plan."
            )
        if path:
            # …and the FILE must still hash to the same revision: a plan edited
            # while the approval was pending cannot ride the old approval.
            try:
                on_disk = Path(path).read_text(encoding="utf-8").strip()
            except OSError:
                on_disk = ""
            if on_disk and _revision_of(on_disk) != revision:
                logger.warning("exit_plan_mode: plan file {} changed during approval", path)
                return (
                    "The plan file changed while the approval was pending, so the "
                    "approval was discarded. Re-submit the current plan."
                )

        decisions = response.get("decisions", []) if isinstance(response, dict) else []
        decision = decisions[0] if decisions else {}
        decision_type = str(decision.get("type") or "")
        message = str(decision.get("message") or "").strip()
        if decision_type in ("approve", "yolo"):
            set_plan_state(session_id, status=PLAN_STATUS_READY, mutation_count=0)
            logger.info("exit_plan_mode: plan {} approved for {}", revision, session_id or "-")
            where = f" It is saved at {path} for later reference." if path else ""
            return (
                f"Plan approved (revision {revision}). Plan mode is OFF and the "
                f"write tools are available again — execute the approved plan.{where} "
                "If execution shows the plan is wrong, call "
                "enter_plan_mode(replan=True)."
            )
        if message:
            mutations += 1
            set_plan_state(session_id, status=PLAN_STATUS_PLANNING, mutation_count=mutations)
            return (
                f'User requested changes: "{message}". Revise the plan and call '
                f"exit_plan_mode again (revision {mutations}/{budget} of the "
                "mutation budget)."
            )
        set_plan_state(session_id, status=PLAN_STATUS_IDLE, mutation_count=0)
        return (
            "User rejected the plan. Decide whether to keep exploring in plan "
            "mode (call enter_plan_mode again) or to stop."
        )

    @override
    def _run(self, plan: str, session_id: str = "", **kwargs: Any) -> str:
        return self._submit(plan, session_id)

    @override
    async def _arun(self, plan: str, session_id: str = "", **kwargs: Any) -> str:
        return self._submit(plan, session_id)


def build_exit_plan_mode_tool() -> ExitPlanModeTool:
    tool: ExitPlanModeTool = ExitPlanModeTool()
    tool.handle_tool_error = True
    return tool
