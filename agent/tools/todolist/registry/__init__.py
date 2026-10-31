"""Registry sub-package: SQLite persistence for session todo rows."""

from .store_sqlite import (
    add_todo,
    append_todo,
    cancel_todos_for_flows,
    complete_todos_for_flows,
    delete_todos_by_session,
    ensure_db,
    get_todo_by_id,
    get_todos,
    get_todos_by_flow,
    get_todos_sync,
    patch_todo_by_id,
    remove_todo_by_id,
    replace_all,
    set_todo_flow_id_by_id,
)

__all__ = [
    "add_todo",
    "append_todo",
    "cancel_todos_for_flows",
    "complete_todos_for_flows",
    "delete_todos_by_session",
    "ensure_db",
    "get_todo_by_id",
    "get_todos",
    "get_todos_by_flow",
    "get_todos_sync",
    "patch_todo_by_id",
    "remove_todo_by_id",
    "replace_all",
    "set_todo_flow_id_by_id",
]
