"""TaskFlow tool family: 8 tools mirroring the openclaw managedFlows API surface.

Mapping: taskflow_create=createManaged, taskflow_run_task=runTask,
taskflow_set_waiting=setWaiting, taskflow_resume=resume,
taskflow_finish=finish, taskflow_fail=fail, taskflow_cancel=requestCancel/cancel,
taskflow_summary=getTaskSummary.
"""

from langchain_core.tools import BaseTool

from .taskflow_cancel import taskflow_cancel
from .taskflow_create import taskflow_create
from .taskflow_fail import taskflow_fail
from .taskflow_finish import taskflow_finish
from .taskflow_resume import taskflow_resume
from .taskflow_run_task import taskflow_run_task
from .taskflow_set_waiting import taskflow_set_waiting
from .taskflow_summary import taskflow_summary

_TASKFLOW_TOOLS: list[BaseTool] = [
    taskflow_create,
    taskflow_run_task,
    taskflow_set_waiting,
    taskflow_resume,
    taskflow_finish,
    taskflow_fail,
    taskflow_cancel,
    taskflow_summary,
]


def build_taskflow_tools() -> list[BaseTool]:
    """Build and return the 8 taskflow tools.

    Meant to be registered in ``_MAIN_TOOLS_BUILDERS``. Business errors are
    returned as readable strings (handle_tool_error=True as backstop), and
    the family is tagged ``scope=main_only``: shared flow state is managed by
    the main agent, matching the taskflow skill's scope, so the subagent
    tool-policy drops it unconditionally.
    """
    for t in _TASKFLOW_TOOLS:
        t.handle_tool_error = True
        t.metadata = {"scope": "main_only"}
    return list(_TASKFLOW_TOOLS)
