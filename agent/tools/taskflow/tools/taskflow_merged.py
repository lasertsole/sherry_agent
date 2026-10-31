"""Three action-dispatch tools over the existing TaskFlow module functions.

The 16 taskflow_* tools are thin wrappers around one SQLite flow store; the
model previously paid for 16 schemas. This module exposes three grouped
surfaces instead:

- ``taskflow_flow``    — lifecycle and budget (create / plan / finish / fail /
  cancel / replan / set_waiting / budget)
- ``taskflow_step``    — step work (run_task / update_steps / dispatch / resume
  / wait_all)
- ``taskflow_inspect`` — read-only reporting (summary / progress / list)

The original module tools stay intact: tests and internal callers keep their
Python entry points (notably ``delivery.py``'s direct ``taskflow_resume``
import), while the merged classes only validate and dispatch. Parameters are
matched against each module function's real signature, so a shared flat model
can never forward ``expected_revision`` to ``taskflow_create`` or a newer
field the dispatcher does not know about.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, ClassVar

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from agent.tools.pub_base import SessionId
from .taskflow_budget import taskflow_budget
from .taskflow_cancel import taskflow_cancel
from .taskflow_create import taskflow_create
from .taskflow_dispatch import taskflow_dispatch
from .taskflow_fail import taskflow_fail
from .taskflow_finish import taskflow_finish
from .taskflow_list import taskflow_list
from .taskflow_plan import taskflow_plan
from .taskflow_progress import taskflow_progress
from .taskflow_replan import taskflow_replan
from .taskflow_resume import taskflow_resume
from .taskflow_run_task import taskflow_run_task
from .taskflow_set_waiting import taskflow_set_waiting
from .taskflow_summary import taskflow_summary
from .taskflow_update_steps import taskflow_update_steps
from .taskflow_wait_all import taskflow_wait_all

FLOW_ACTIONS = (
    "create",
    "plan",
    "finish",
    "fail",
    "cancel",
    "replan",
    "set_waiting",
    "budget",
)
STEP_ACTIONS = ("run_task", "update_steps", "dispatch", "resume", "wait_all")
INSPECT_ACTIONS = ("summary", "progress", "list")

_FLOW_TOOLS: dict[str, BaseTool] = {
    "create": taskflow_create,
    "plan": taskflow_plan,
    "finish": taskflow_finish,
    "fail": taskflow_fail,
    "cancel": taskflow_cancel,
    "replan": taskflow_replan,
    "set_waiting": taskflow_set_waiting,
    "budget": taskflow_budget,
}
_STEP_TOOLS: dict[str, BaseTool] = {
    "run_task": taskflow_run_task,
    "update_steps": taskflow_update_steps,
    "dispatch": taskflow_dispatch,
    "resume": taskflow_resume,
    "wait_all": taskflow_wait_all,
}
_INSPECT_TOOLS: dict[str, BaseTool] = {
    "summary": taskflow_summary,
    "progress": taskflow_progress,
    "list": taskflow_list,
}
_GLOBAL_ACTION_TOOL: dict[str, str] = {
    **{action: "taskflow_flow" for action in FLOW_ACTIONS},
    **{action: "taskflow_step" for action in STEP_ACTIONS},
    **{action: "taskflow_inspect" for action in INSPECT_ACTIONS},
}

# Signatures cannot express "empty string is missing" for these. A group means
# "at least one of these must be present" (resume accepts a key or a result).
_ONE_OF_REQUIRED: dict[str, tuple[tuple[str, ...], ...]] = {
    "resume": (("child_session_key", "result"),),
}
# The old taskflow_budget surface took action=query|set. Keep those accepted
# as aliases so a model that remembers the old shape reaches the budget action
# instead of burning a retry on an unknown action.
_BUDGET_ACTION_ALIASES: set[str] = {"query", "set"}


class TaskflowFlowInput(BaseModel):
    """Lifecycle and budget parameters; only the action's own fields are used."""

    action: str = Field(
        description=(
            "One of: create, plan, finish, fail, cancel, replan, set_waiting, budget. "
            "Required fields by action: create→flow_id; plan→goal; "
            "finish/fail/cancel/set_waiting/replan→flow_id; budget→flow_id."
        ),
        json_schema_extra={
            "enum": [
                "create",
                "plan",
                "finish",
                "fail",
                "cancel",
                "replan",
                "set_waiting",
                "budget",
            ]
        },
    )
    flow_id: str | None = Field(
        default=None,
        description="Flow id. create: a new id; plan: optional existing/planned id; every other action: required.",
    )
    goal: str | None = Field(default=None, description="Goal to decompose into steps (plan).")
    description: str | None = Field(
        default=None, description="Human-readable flow description (create)."
    )
    ack_dup: bool | None = Field(
        default=None,
        description=(
            "create/plan: set True to skip the duplicate-goal check after reviewing "
            "the possible-duplicate warning."
        ),
    )
    initial_state: dict | None = Field(
        default=None, description="Initial state object stored on the flow (create)."
    )
    deadline_hours: float | None = Field(
        default=None,
        description="Overall deadline in hours; an expired flow is failed by the sweeper (create).",
    )
    resumed_from: str | None = Field(
        default=None,
        description="Predecessor flow id this flow replaces; the predecessor is marked superseded (create).",
    )
    todo_content: str | None = Field(
        default=None,
        description="Pending todo that roots the flow's GC; defaults to description (create).",
    )
    todo_category: str | None = Field(
        default=None, description="Category of the root todo; default quick (create)."
    )
    context: str | None = Field(default=None, description="Extra planning context (plan).")
    max_steps: int | None = Field(
        default=None, description="Maximum planned steps; default 8 (plan)."
    )
    summary: str | None = Field(default=None, description="Completion summary (finish).")
    reason: str | None = Field(
        default=None, description="Failure, cancellation, or replan reason (fail/cancel/replan)."
    )
    wait_reason: str | None = Field(
        default=None, description="What the flow is waiting for (set_waiting)."
    )
    blocked_step_id: str | None = Field(
        default=None, description="Step that triggered the replan (replan)."
    )
    keep_done: bool | None = Field(
        default=None, description="Keep completed steps unchanged on replan; default true."
    )
    expected_revision: int | None = Field(
        default=None,
        description="Optimistic-lock revision from taskflow_inspect; pass on mutations to fail fast on concurrent writers.",
    )
    budget_action: str | None = Field(
        default=None, description="For action=budget only: query (default) or set."
    )
    token_budget: int | None = Field(
        default=None, description="Positive token cap for budget_action=set."
    )
    session_id: SessionId = ""


class TaskflowStepInput(BaseModel):
    """Step execution parameters; only the action's own fields are used."""

    action: str = Field(
        description=(
            "One of: run_task, update_steps, dispatch, resume, wait_all. "
            "Required fields by action: run_task→flow_id,task; update_steps→flow_id,steps; "
            "dispatch→flow_id,step_ids; resume→flow_id,child_session_key or result; wait_all→flow_id."
        ),
        json_schema_extra={"enum": ["run_task", "update_steps", "dispatch", "resume", "wait_all"]},
    )
    flow_id: str | None = Field(default=None, description="Target flow id.")
    task: str | None = Field(
        default=None, description="Step task text dispatched to the child (run_task)."
    )
    label: str | None = Field(default=None, description="Short step label (run_task).")
    expected_revision: int | None = Field(
        default=None,
        description="Optimistic-lock revision from taskflow_inspect; pass on mutations to fail fast on concurrent writers.",
    )
    depends_on: list[str] | None = Field(
        default=None,
        description="Prerequisite step ids; the step stays blocked until every one is done (run_task/update_steps).",
    )
    validation_criteria: str | None = Field(
        default=None,
        description="Natural-language acceptance criteria, judged on resume (run_task/resume).",
    )
    retry_policy: dict | None = Field(
        default=None,
        description="Failure-aware retry: {max_retries, retry_delay_seconds, retry_on} (run_task).",
    )
    aggregate_deps: bool | None = Field(
        default=None,
        description="Append dependency results to the dispatched task text (synthesis step).",
    )
    response_schema: dict | None = Field(
        default=None,
        description="JSON Schema the child's structured result must satisfy (Tier 1 gate).",
    )
    expected_params: dict | None = Field(
        default=None, description="Structured inputs recorded for the report (run_task)."
    )
    input_bindings: dict | None = Field(
        default=None,
        description="Map a param to 'step-A.structured_result.files' to append upstream values to the task (run_task).",
    )
    file_deps: list[str] | None = Field(
        default=None,
        description="Project-relative source files whose changes should trigger re-verification of this step.",
    )
    judge_criteria: str | None = Field(
        default=None, description="Semantic acceptance criteria for the step judge (Tier 2)."
    )
    judge_model: str | None = Field(
        default=None, description="Optional judge model override (run_task/resume)."
    )
    functional_role: str | None = Field(
        default=None,
        description="Child role: researcher / executor / reviewer / librarian / general.",
    )
    step_model: str | None = Field(
        default=None, description="Optional child model override (run_task)."
    )
    step_timeout_seconds: float | None = Field(
        default=None, description="Per-step child wall-clock timeout (run_task)."
    )
    priority: int | None = Field(default=None, description="Dispatch ordering hint (run_task).")
    child_session_key: str | None = Field(
        default=None, description="Completed child session key (resume)."
    )
    result: str | None = Field(default=None, description="Child result text to inject (resume).")
    token_usage: dict | None = Field(
        default=None,
        description="Child spend: {input_tokens, output_tokens, model_name} (resume).",
    )
    structured_result: dict | None = Field(
        default=None,
        description="Pre-parsed structured result to validate instead of parsing result text (resume).",
    )
    step_outcome: str | None = Field(
        default=None,
        description="success / failure / partial / skipped; outranks the result gates (resume).",
    )
    auto_dispatch: bool | None = Field(
        default=None, description="Dispatch newly unlocked steps after resume; default true."
    )
    steps: list[dict] | None = Field(
        default=None,
        description=(
            "COMPLETE replacement step list (update_steps). Each step needs step_id, task, "
            "depends_on, status (ready|blocked|dispatched|done)."
        ),
    )
    step_ids: list[str] | None = Field(
        default=None, description="Ready step ids to dispatch (dispatch)."
    )
    timeout_seconds: float | None = Field(
        default=None, description="wait_all timeout; default 300."
    )
    poll_interval_seconds: float | None = Field(
        default=None, description="wait_all poll interval; default 0.5."
    )
    session_id: SessionId = ""


class TaskflowInspectInput(BaseModel):
    """Read-only reporting parameters."""

    action: str = Field(
        description=(
            "One of: summary, progress, list. Required fields by action: "
            "summary→flow_id; progress→flow_id; list→none."
        ),
        json_schema_extra={"enum": ["summary", "progress", "list"]},
    )
    flow_id: str | None = Field(default=None, description="Target flow id (summary/progress).")
    status_filter: str | None = Field(
        default=None,
        description="list filter: active (running+waiting, default) | all | running | waiting | done | failed | cancelled.",
    )
    session_id: SessionId = ""


def _accepted_and_required(tool: BaseTool, action: str) -> tuple[set[str], list[str]]:
    """Read the module function's real signature, not a hand-maintained table."""
    parameters = inspect.signature(tool.coroutine).parameters
    accepted = {name for name in parameters if name not in {"session_id", "run_manager"}}
    required = [
        name
        for name, parameter in parameters.items()
        if name not in {"session_id", "run_manager"}
        and parameter.default is inspect.Parameter.empty
    ]
    return accepted, required


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == []


def _error(tool_name: str, action: str, message: str, accepted: set[str] | None = None) -> str:
    suffix = f" Accepted parameters: {', '.join(sorted(accepted))}." if accepted else ""
    return f"Error: {tool_name} action='{action}': {message}.{suffix}"


class _MergedTaskflowTool(BaseTool):
    """Shared validation + dispatch for the three grouped tools."""

    action_tools: ClassVar[dict[str, BaseTool]] = {}
    tool_label: ClassVar[str] = "taskflow"

    def _unknown_action(self, action: str) -> str:
        if action in _BUDGET_ACTION_ALIASES:
            return (
                f"Error: {self.name} does not support action '{action}'. "
                f"Use taskflow_flow(action='budget', budget_action='{action}') instead."
            )
        suggestion = _GLOBAL_ACTION_TOOL.get(action)
        if suggestion and suggestion != self.name:
            return (
                f"Error: {self.name} does not support action '{action}'. "
                f"Use {suggestion}(action='{action}') instead."
            )
        return (
            f"Error: {self.name} has unknown action '{action}'. "
            f"Available: {', '.join(sorted(self.action_tools))}."
        )

    def _normalize_action(self, action: str, kwargs: dict[str, Any]) -> str:
        """Accept the old taskflow_budget(action=query|set) shape as an alias."""
        if self.name == "taskflow_flow" and action in _BUDGET_ACTION_ALIASES:
            if not kwargs.get("budget_action"):
                kwargs["budget_action"] = action
            return "budget"
        return action

    def _resolve(
        self, action: str, session_id: str, kwargs: dict[str, Any]
    ) -> tuple[dict, str | None]:
        tool = self.action_tools.get(action)
        if tool is None:
            return {}, self._unknown_action(action)
        accepted, required = _accepted_and_required(tool, action)
        payload: dict[str, Any] = {}
        for name in accepted:
            if name == "action":
                continue  # the module's own action (budget) is mapped below
            value = kwargs.get(name)
            if value is not None:
                payload[name] = value
        if action == "budget" and "action" in accepted and kwargs.get("budget_action") is not None:
            payload["action"] = kwargs["budget_action"]
        missing = [name for name in required if _is_missing(payload.get(name))]
        if not missing:
            for group in _ONE_OF_REQUIRED.get(action, ()):
                if all(_is_missing(payload.get(name)) for name in group):
                    missing.append(" or ".join(group))
        if missing:
            return payload, _error(
                self.name,
                action,
                f"missing required parameter(s): {', '.join(missing)}",
                accepted,
            )
        payload["session_id"] = session_id
        return payload, None

    async def _dispatch_async(self, action: str, session_id: str, kwargs: dict[str, Any]) -> str:
        action = self._normalize_action(action, kwargs)
        payload, error = self._resolve(action, session_id, kwargs)
        if error is not None:
            return error
        tool = self.action_tools[action]
        try:
            result = await tool.coroutine(**payload)
        except Exception as exc:  # noqa: BLE001 - tool boundary returns readable text
            return f"Error: {self.name} action='{action}' failed: {type(exc).__name__}: {exc}"
        return str(result)

    def _run(self, action: str, session_id: str = "", **kwargs: Any) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._dispatch_async(action, session_id, kwargs))
        return (
            f"Error: {self.name} is async; call it with await/ainvoke instead of a "
            "synchronous invoke inside a running event loop."
        )

    async def _arun(self, action: str, session_id: str = "", **kwargs: Any) -> str:
        return await self._dispatch_async(action, session_id, kwargs)


class TaskflowFlowTool(_MergedTaskflowTool):
    name: str = "taskflow_flow"
    description: str = (
        "TaskFlow lifecycle and budget. Choose action, then pass that action's fields. "
        "Required: create→flow_id; plan→goal; finish/fail/cancel/set_waiting/replan→flow_id; "
        "budget→flow_id (use budget_action=query|set, plus token_budget when set). "
        "Pass expected_revision on mutations to fail fast; a conflict returns the latest revision to inspect. "
        "create/plan warn about a similar ACTIVE flow in this session: reuse it, or re-call with ack_dup=true "
        "when it is genuinely a different task. Terminal actions reject later mutations."
    )
    args_schema: type[BaseModel] = TaskflowFlowInput
    action_tools: ClassVar[dict[str, BaseTool]] = _FLOW_TOOLS
    tool_label: ClassVar[str] = "taskflow_flow"


class TaskflowStepTool(_MergedTaskflowTool):
    name: str = "taskflow_step"
    description: str = (
        "TaskFlow step execution. Choose action, then pass that action's fields. "
        "Required: run_task→flow_id,task; update_steps→flow_id,steps (COMPLETE list); "
        "dispatch→flow_id,step_ids; resume→flow_id,child_session_key or result; wait_all→flow_id. "
        "run_task dispatches a detached child; depends_on blocks until prerequisites are done; "
        "response_schema/judge_criteria gate the result on resume; wait_all polls only this flow's children."
    )
    args_schema: type[BaseModel] = TaskflowStepInput
    action_tools: ClassVar[dict[str, BaseTool]] = _STEP_TOOLS
    tool_label: ClassVar[str] = "taskflow_step"


class TaskflowInspectTool(_MergedTaskflowTool):
    name: str = "taskflow_inspect"
    description: str = (
        "Read-only TaskFlow reporting, safe to run alongside other tools. Choose action: "
        "summary (flow_id) returns status, revision, steps and results; "
        "progress (flow_id) returns completion percentage, counts and next steps; "
        "list (optional status_filter) returns this session's flows."
    )
    args_schema: type[BaseModel] = TaskflowInspectInput
    action_tools: ClassVar[dict[str, BaseTool]] = _INSPECT_TOOLS
    tool_label: ClassVar[str] = "taskflow_inspect"


TASKFLOW_MERGED_TOOLS: list[BaseTool] = [
    TaskflowFlowTool(),
    TaskflowStepTool(),
    TaskflowInspectTool(),
]

__all__ = [
    "FLOW_ACTIONS",
    "INSPECT_ACTIONS",
    "STEP_ACTIONS",
    "TASKFLOW_MERGED_TOOLS",
    "TaskflowFlowInput",
    "TaskflowFlowTool",
    "TaskflowInspectInput",
    "TaskflowInspectTool",
    "TaskflowStepInput",
    "TaskflowStepTool",
]
