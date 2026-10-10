"""The main agent's tool catalogue: every tool the session may switch on/off.

One place knows the group a tool belongs to, the display order of the groups, and
which tools are REQUIRED, so the 预设-工具 tab (served over ``GET /agent/catalog``)
never hardcodes backend names and a newly added builder cannot silently fall into
an unlabelled bucket: a test walks the REAL ``build_main_tools()`` output and
fails when a tool is missing from this map.

Three rules the catalogue carries:

* :data:`TOOL_ORDER` — the group order the UI renders (core working sets first);
* :data:`REQUIRED_TOOLS` — tools no session config may drop (the workspace, code
  execution, skills and the user-interaction channel are what makes the agent
  able to work at all); the service rejects a payload omitting one, and
  :mod:`agent.middlewares.tool_selection` always keeps them enabled even when a
  register value predates the rule;
* :data:`BULK_ONLY_GROUPS` — groups the UI only offers as select-all / clear-all
  (their membership moves together; per-tool switches would invite a half-broken
  task/subagent surface).
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "BULK_ONLY_GROUPS",
    "REQUIRED_TOOLS",
    # The catalogue lists the MAIN agent's tools. The subagent-only families
    # (code_intel explore/semantic, ast_grep, lsp, ptc) are deliberately absent:
    # they are registered per child at spawn (``build_code_intel_tools`` and friends
    # are called from the spawn path), so they never appear in the main tool face
    # nor in ``GET /agent/catalog``. A new subagent tool joins them there, not here.
    "TOOL_GROUPS",
    "TOOL_ORDER",
    "tool_catalog",
    "tool_required",
]

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
        "taskflow_replan",
        "taskflow_plan",
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
    "browser": (
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_press",
        "browser_scroll",
        "browser_screenshot",
        "browser_evaluate",
    ),
}

#: Group for tools that appear at runtime (MCP servers, future builders) —
#: matches the ``mcp__<server>__<tool>`` naming those tools use.
_FALLBACK_GROUP = "mcp"

#: Display order of the groups: the routes the agent needs to do any work come
#: first (files / code execution / skills / asking the user), then memory, then
#: the task and delegation surfaces. Groups outside this list keep their
#: ``TOOL_GROUPS`` order after these.
TOOL_ORDER: tuple[str, ...] = (
    "skills",
    "terminal",
    "files",
    "interaction",
    "memory",
    "tasks",
    "subagents",
    "web",
    "browser",
    "mcp",
)

#: Tools no session config may drop. Locked in the UI, refused by the service
#: (``PUT /sessions/agent_config`` 400s when the payload omits one) and unioned
#: back in by ``ToolSelectionMiddleware`` so a stale register value cannot drop
#: them either: without the workspace, code execution, skill loading or the
#: question channel the agent cannot work at all. ``message_search`` is required
#: beside the optional ``memory`` — a session may forget its preferences but must
#: stay able to look its own transcript up.
REQUIRED_TOOLS: frozenset[str] = frozenset(
    {
        # files
        "read_file",
        "write_file",
        "patch_file",
        "search_files",
        # terminal & code
        "terminal",
        "python_repl",
        # skills
        "skill_manage",
        "skill_list",
        "skill_view",
        # interaction
        "question",
        # memory (the search half only; `memory` stays switchable)
        "message_search",
    }
)

#: Groups the UI offers as select-all / clear-all only (no per-tool switches).
BULK_ONLY_GROUPS: frozenset[str] = frozenset({"tasks", "subagents"})


def tool_required(name: str) -> bool:
    """Whether *name* may never be left out of a session's tool set."""
    return name in REQUIRED_TOOLS


def _group_rank(group: str) -> int:
    """Sort key for a group id (see :data:`TOOL_ORDER`)."""
    try:
        return TOOL_ORDER.index(group)
    except ValueError:
        return len(TOOL_ORDER)


def _group_of(name: str) -> str:
    """Group id for one tool name (static map, then the MCP prefix rule)."""
    for group, names in TOOL_GROUPS.items():
        if name in names:
            return group
    return _FALLBACK_GROUP


#: Longest description the catalogue serves (the UI shows it as a hover tooltip;
#: a docstring's first line is what a reader needs, and the full text stays in
#: the source).
_DESCRIPTION_MAX_CHARS = 240


def _description_of(tool: Any) -> str:
    """The tool's own first description line, bounded (``""`` when it has none).

    Served verbatim — it is the truest summary of what the tool does, including
    runtime state the client cannot know (``web_search`` reports a missing API
    key in its own description).
    """
    raw = getattr(tool, "description", "") or ""
    first_line = str(raw).strip().split("\n", 1)[0].strip()
    return first_line[:_DESCRIPTION_MAX_CHARS]


def tool_catalog(tools: list[Any] | None = None) -> list[dict[str, Any]]:
    """``[{"name", "group", "description", "required"}]`` for every main-agent
    tool, in the catalogue's group order (see :data:`TOOL_ORDER`).

    ``tools`` injects the tool list (tests that run where ``agent.tools`` is
    stubbed, e.g. beside the subagent suite, pass the REAL builders' output);
    the default builds the real process-wide list.
    """
    if tools is None:
        from . import build_main_tools  # lazy: the package is stubbed in some test processes

        tools = build_main_tools()
    entries = [
        {
            "name": tool.name,
            "group": _group_of(tool.name),
            "description": _description_of(tool),
            "required": tool_required(tool.name),
        }
        for tool in tools
    ]
    # Grouped display order (a stable sort keeps the build order inside a group).
    return sorted(entries, key=lambda entry: _group_rank(entry["group"]))
