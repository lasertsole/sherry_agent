"""The main agent's tool catalogue: every tool the session may switch on/off.

One place knows the group a tool belongs to, so the 预设-工具 tab (served over
``GET /agent/catalog``) never hardcodes backend names and a newly added builder
cannot silently fall into an unlabelled bucket: a test walks the REAL
``build_main_tools()`` output and fails when a tool is missing from this map.
"""

from __future__ import annotations

from typing import Any

__all__ = ["TOOL_GROUPS", "tool_catalog"]

#: Group id → the tools in it. Group ids are i18n keys on the client
#: (``config.agent.toolGroup.<id>``), so a new group needs a label there.
TOOL_GROUPS: dict[str, tuple[str, ...]] = {
    "files": ("read_file", "write_file", "patch_file", "search_files"),
    "terminal": ("terminal", "python_repl"),
    "tasks": (
        "taskflow_create",
        "taskflow_run_task",
        "taskflow_set_waiting",
        "taskflow_resume",
        "taskflow_finish",
        "taskflow_fail",
        "taskflow_cancel",
        "taskflow_summary",
        "taskflow_progress",
        "taskflow_budget",
        "taskflow_dispatch",
        "taskflow_update_steps",
        "taskflow_wait_all",
        "taskflow_list",
        "todowrite",
        "todoread",
        "knowledge",
    ),
    "memory": ("memory", "message_search"),
    "skills": ("skill_manage", "skill_list", "skill_view"),
    "subagents": (
        "sessions_spawn",
        "sessions_yield",
        "sessions_send",
        "sessions_kill",
        "sessions_steer",
        "agents_list",
        "subagents_list",
    ),
    "interaction": ("question",),
    "web": ("web_search",),
}

#: Group for tools that appear at runtime (MCP servers, future builders) —
#: matches the ``mcp__<server>__<tool>`` naming those tools use.
_FALLBACK_GROUP = "mcp"


def _group_of(name: str) -> str:
    """Group id for one tool name (static map, then the MCP prefix rule)."""
    for group, names in TOOL_GROUPS.items():
        if name in names:
            return group
    return _FALLBACK_GROUP


def tool_catalog(tools: list[Any] | None = None) -> list[dict[str, str]]:
    """``[{"name": ..., "group": ...}]`` for every main-agent tool, in build order.

    ``tools`` injects the tool list (tests that run where ``agent.tools`` is
    stubbed, e.g. beside the subagent suite, pass the REAL builders' output);
    the default builds the real process-wide list.
    """
    if tools is None:
        from . import build_main_tools  # lazy: the package is stubbed in some test processes

        tools = build_main_tools()
    return [{"name": tool.name, "group": _group_of(tool.name)} for tool in tools]
