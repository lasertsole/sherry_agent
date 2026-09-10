"""TodoList tool family: session-scoped, compression-proof task checklist.

This package owns the persistent todo list for one session (``todos.db``) plus
the delegation metadata that points at TaskFlow flows/steps. It deliberately
owns NO dependency graph or scheduler: DAG edges, unlock logic and the
``blocked/ready/dispatched/done`` step status live in TaskFlow and are reached
through ``flow_id``/``step_id``.

Phase 1 (this commit) provides the store layer; the service, tools and
middlewares are added by later phases. Import the persistence API from
``agent.tools.todolist.registry``.
"""
