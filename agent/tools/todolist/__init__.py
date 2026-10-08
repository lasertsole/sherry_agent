"""TodoList tool family: session-scoped, compression-proof task checklist.

This package owns the persistent todo list for one session (``todos.db``) plus
the delegation metadata that points at TaskFlow flows/steps. It deliberately
owns NO dependency graph or scheduler: DAG edges, unlock logic and the
``blocked/ready/dispatched/done`` step status live in TaskFlow and are reached
through ``flow_id``/``step_id``.

Public surface: the store/service layers and the ``build_todolist_tools``
builder registered in ``agent.tools._MAIN_TOOLS_BUILDERS``. Usage:

    from agent.tools.todolist import build_todolist_tools
"""

from .tools import build_todolist_tools

__all__ = ["build_todolist_tools"]
