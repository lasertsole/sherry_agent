"""TaskFlow tool family.

Production exposes three grouped tools — ``taskflow_flow``,
``taskflow_step`` and ``taskflow_inspect`` — over the same 16 module functions
(create/plan/resume/...). The module tools remain available for internal Python
callers (notably ``delivery.py``'s direct ``taskflow_resume`` import) and for
tests via :func:`build_legacy_taskflow_tools`; only the model-facing surface is
merged.
"""

from langchain_core.tools import BaseTool

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
from .taskflow_merged import TASKFLOW_MERGED_TOOLS

# The original module-level tools, retained for direct Python callers and the
# test adapter. They are NOT registered in the model face.
_TASKFLOW_MODULE_TOOLS: list[BaseTool] = [
    taskflow_create,
    taskflow_run_task,
    taskflow_set_waiting,
    taskflow_resume,
    taskflow_finish,
    taskflow_fail,
    taskflow_cancel,
    taskflow_summary,
    taskflow_progress,
    taskflow_budget,
    taskflow_dispatch,
    taskflow_update_steps,
    taskflow_wait_all,
    taskflow_list,
    taskflow_replan,
    taskflow_plan,
]

# Pure reporting tools: the guardrails may hash their results to detect a
# no-progress loop. The merged inspect tool keeps that property; the lifecycle
# and step tools mutate flow state and stay unsafe barriers.
_READ_ONLY_TASKFLOW: set[str] = {"taskflow_inspect"}
# Grouped tools whose SOME actions are repeat-safe. The guardrail hashes the
# result only for these actions: taskflow_create/taskflow_plan return a stable
# duplicate answer, taskflow_update_steps is a full replacement and
# taskflow_resume explicitly treats a duplicate delivery as a no-op. Everything
# else (dispatch/finish/fail/cancel/replan/set_waiting/budget and run_task)
# changes state or errors on repetition, so it stays non-idempotent.
_IDEMPOTENT_ACTIONS_TASKFLOW: dict[str, set[str]] = {
    "taskflow_flow": {"create", "plan"},
    "taskflow_step": {"update_steps", "resume"},
}
# The legacy adapter keeps the original read/write split for the tests and
# internal callers that still drive the module tools directly.
_READ_ONLY_LEGACY_TASKFLOW: set[str] = {
    "taskflow_summary",
    "taskflow_progress",
    "taskflow_list",
}


def _tag_metadata(
    tools: list[BaseTool],
    read_only: set[str],
    idempotent_actions: dict[str, set[str]] | None = None,
) -> None:
    for t in tools:
        t.handle_tool_error = True
        metadata = {
            **(t.metadata or {}),
            "scope": "main_only",
            "idempotent": t.name in read_only,
            "side_effect_scope": "none" if t.name in read_only else "session",
        }
        actions = (idempotent_actions or {}).get(t.name)
        if actions:
            metadata["idempotent_actions"] = tuple(sorted(actions))
        t.metadata = metadata


def build_taskflow_tools() -> list[BaseTool]:
    """Build and return the three merged TaskFlow tools.

    Meant to be registered in ``_MAIN_TOOLS_BUILDERS``. Business errors are
    returned as readable strings (handle_tool_error=True as backstop), and
    the family is tagged ``scope=main_only``: shared flow state is managed by
    the main agent, so the subagent tool-policy drops it unconditionally.
    ``taskflow_inspect`` stays idempotent/read-only; the mutating two are
    unsafe barriers, with their repeat-safe ACTIONS declared separately in
    ``idempotent_actions`` so a retried create/plan/update_steps/resume does
    not look like progress.
    """
    _tag_metadata(TASKFLOW_MERGED_TOOLS, _READ_ONLY_TASKFLOW, _IDEMPOTENT_ACTIONS_TASKFLOW)
    return list(TASKFLOW_MERGED_TOOLS)


def build_legacy_taskflow_tools() -> list[BaseTool]:
    """Return the original 16 module tools for tests/direct compatibility.

    Not registered in the model face; production code should use
    :func:`build_taskflow_tools` or the module-level functions directly. The
    metadata is tagged exactly like the merged family so old tests keep their
    guardrail assertions.
    """
    _tag_metadata(_TASKFLOW_MODULE_TOOLS, _READ_ONLY_LEGACY_TASKFLOW)
    return list(_TASKFLOW_MODULE_TOOLS)
