"""Static parallel-safety classification and the file-aware batch plan.

A tool call is parallel-safe only when it is explicitly read-only or declares
``concurrent_safe``; unknown metadata defaults to unsafe. File tools are
different from the rest: two calls to the SAME canonical path conflict only when
at least one is a write, while calls to different paths are independent. The
batch plan below encodes that distinction for ``SafeToolNode``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.path import resolve_default_project_dir

from .pub_base import session_workspace_root

__all__ = [
    "BatchGroup",
    "BatchPlan",
    "FILE_READ_TOOLS",
    "FILE_WRITE_TOOLS",
    "READ_ONLY_TOOLS",
    "file_resource_key",
    "plan_batch",
    "tool_is_parallel_safe",
]

#: Tools with no side effects; safe to run alongside anything.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "search_files",
        "explore",
        "callers",
        "callees",
        "impact",
        "semantic_code_search",
        "ast_grep_search",
        "lsp_goto_definition",
        "lsp_find_references",
        "lsp_workspace_symbol",
        "lsp_call_hierarchy",
        "lsp_diagnostics",
        "lsp_status",
        "web_search",
        "web_fetch",
        "message_search",
        "todoread",
        "taskflow_summary",
        "taskflow_progress",
        "taskflow_list",
        "terminal_bg_list",
        "terminal_bg_output",
        "skill_list",
        "skill_view",
        "agents_list",
        "subagents_list",
        "sessions_messages",
    }
)

#: File tools split by their file mode. ``read_file`` is also in
#: READ_ONLY_TOOLS, but the file-aware scheduler checks these sets first.
FILE_READ_TOOLS: frozenset[str] = frozenset({"read_file"})
FILE_WRITE_TOOLS: frozenset[str] = frozenset({"write_file", "patch_file"})


@dataclass(frozen=True, slots=True)
class BatchGroup:
    """A set of call indices executed together.

    ``concurrent`` means the indices may start at the same time (all-read file
    group or a safe call); otherwise they run in original order.
    """

    indices: tuple[int, ...]
    concurrent: bool


@dataclass(frozen=True, slots=True)
class BatchPlan:
    """The batch execution plan: groups plus the reason a barrier was chosen."""

    groups: tuple[BatchGroup, ...]
    barrier: bool
    reason: str = ""
    file_paths: tuple[str, ...] = ()


def _state_session_id(state: Any) -> str | None:
    if isinstance(state, dict):
        value = state.get("session_id")
        return value if isinstance(value, str) and value else None
    value = getattr(state, "session_id", None)
    return value if isinstance(value, str) and value else None


def _canonical_path(file_path: str, state: Any) -> str | None:
    """Resolve a file argument without triggering the external-path HITL gate.

    The actual tool resolves and gates the path again. This key only groups
    same-target calls, so a failure here means "resource unknown" → barrier.
    """
    raw = file_path.strip()
    if not raw:
        return None
    try:
        path = Path(os.path.expanduser(raw))
        if not path.is_absolute():
            root = session_workspace_root(_state_session_id(state))
            path = (root if root is not None else resolve_default_project_dir()) / path
        return str(path.resolve())
    except (OSError, RuntimeError, ValueError):
        return None


def file_resource_key(call: dict[str, Any], state: Any) -> tuple[str, str] | None:
    """``(canonical_path, mode)`` for a file tool call, else ``None``.

    ``mode`` is ``"read"`` for ``read_file`` and ``"write"`` for
    ``write_file`` / ``patch_file``.
    """
    name = str(call.get("name") or "")
    if name not in FILE_READ_TOOLS and name not in FILE_WRITE_TOOLS:
        return None
    args = call.get("args")
    if not isinstance(args, dict):
        return None
    file_path = args.get("file_path")
    if not isinstance(file_path, str):
        return None
    resolved = _canonical_path(file_path, state)
    if resolved is None:
        return None
    return resolved, ("read" if name in FILE_READ_TOOLS else "write")


def tool_is_parallel_safe(name: str, tool: Any) -> bool:
    """True only for an explicit read-only / concurrent-safe declaration."""
    if name in READ_ONLY_TOOLS:
        return True
    meta = getattr(tool, "metadata", None)
    if not isinstance(meta, dict):
        return False
    if meta.get("destructive"):
        return False
    if meta.get("concurrent_safe"):
        return True
    return meta.get("side_effect_scope") == "none"


def plan_batch(
    tool_calls: list[dict[str, Any]],
    tools_by_name: dict[str, Any],
    state: Any,
) -> BatchPlan:
    """Build the file-aware execution plan for one ToolNode batch.

    - Any non-file unsafe call is a barrier: the whole batch runs in order.
    - Otherwise file calls group by canonical path. A group with a write runs
      in call order; an all-read group may run concurrently. Safe calls run
      concurrently as singleton groups.
    - A file path that cannot be resolved makes the whole batch a barrier.
    """
    if not tool_calls:
        return BatchPlan(groups=(), barrier=False)

    file_groups: dict[str, list[int]] = {}
    file_has_write: dict[str, bool] = {}
    file_paths: list[str] = []
    safe_indices: list[int] = []

    for index, call in enumerate(tool_calls):
        name = str(call.get("name") or "unknown")
        if name in FILE_READ_TOOLS or name in FILE_WRITE_TOOLS:
            key = file_resource_key(call, state)
            if key is None:
                return BatchPlan(
                    groups=(BatchGroup(tuple(range(len(tool_calls))), False),),
                    barrier=True,
                    reason=f"{name} has an unresolvable file path",
                    file_paths=tuple(file_paths),
                )
            path, mode = key
            if path not in file_groups:
                file_groups[path] = []
                file_has_write[path] = False
                file_paths.append(path)
            file_groups[path].append(index)
            file_has_write[path] = file_has_write[path] or mode == "write"
            continue

        if not tool_is_parallel_safe(name, tools_by_name.get(name)):
            return BatchPlan(
                groups=(BatchGroup(tuple(range(len(tool_calls))), False),),
                barrier=True,
                reason=f"{name} is not parallel-safe",
                file_paths=tuple(file_paths),
            )
        safe_indices.append(index)

    groups: list[BatchGroup] = [
        BatchGroup(tuple(indices), not file_has_write[path])
        for path, indices in file_groups.items()
    ]
    groups.extend(BatchGroup((index,), True) for index in safe_indices)
    groups.sort(key=lambda group: group.indices[0])
    return BatchPlan(groups=tuple(groups), barrier=False, file_paths=tuple(file_paths))
