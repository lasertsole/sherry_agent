"""Background terminal tasks: the caps the registry and the tools obey."""

from typing import TypedDict


class TerminalBgConfig(TypedDict):
    """Bounds for ``terminal(run_in_background=True)`` and its query tool."""

    #: Live background tasks one session may keep at once; a further spawn is
    #: refused (with the oldest finished task's id) rather than silently
    #: unbounded — each one is a real OS process.
    max_tasks_per_session: int
    #: How much of a task's output file ``terminal_bg(action='output')`` returns: the TAIL
    #: (the last output is what a long-running command's status lives in).
    output_max_read_bytes: int
    #: Exit-notification behaviour: push the WS frame and wake the agent with an
    #: auto-turn when a background task exits.
    notify_on_exit: bool


TERMINAL_BG: TerminalBgConfig = {
    "max_tasks_per_session": 10,
    "output_max_read_bytes": 8192,
    "notify_on_exit": True,
}
