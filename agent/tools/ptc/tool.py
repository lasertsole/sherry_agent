"""The ``execute_code`` LangChain tool (Programmatic Tool Calling).

This tool is injected **only** into ``FunctionalRole.EXECUTOR`` subagents. It
is never part of ``_MAIN_TOOLS_BUILDERS`` and never reaches the main agent or
any other functional role. Its own whitelist excludes ``execute_code`` so a
generated script cannot recurse into PTC.
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForToolRun,
    CallbackManagerForToolRun,
)
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, PrivateAttr

from config.features import PTC

from .runner import run_ptc
from .stub_generator import ToolStub, tool_specs_from_base_tools


class ExecuteCodeInput(BaseModel):
    """Arguments for ``execute_code``.

    ``session_id`` is deliberately NOT a parameter — it is bound at tool
    construction from the child's ``child_session_key`` so a script can never
    supply or spoof a session.
    """

    code: str = Field(description="Python source to execute in the isolated PTC child process.")


def _render_signature(stub: ToolStub) -> str:
    parts = []
    for name, default in stub.params:
        parts.append(name if default is None else f"{name}={default}")
    return f"{stub.name}({', '.join(parts)})"


def _build_description(tools_map: dict[str, Any]) -> str:
    lines = [
        "Run a Python script that can call Sherry tools programmatically.",
        "Use this when you need 3+ tool calls with processing logic between them, "
        "need to filter/reduce large tool outputs before they enter your context, "
        "need conditional branching, or need to loop over tool calls.",
        "Use normal tool calls instead for a single call, tasks needing complex "
        "reasoning over the full result, or interactive user input.",
    ]
    stubs = tool_specs_from_base_tools(tools_map.values())
    if stubs:
        lines.append("")
        lines.append("Available via `from sherry_tools import ...`:")
        lines.extend(f"  {_render_signature(stub)}" for stub in stubs)
    lines.extend(
        [
            "",
            "Also built into `sherry_tools` (no import needed):",
            "  json_parse(text) — json.loads with strict=False",
            "  shell_quote(s) — shlex.quote",
            "  retry(fn, max_attempts=3, delay=2) — retry with exponential backoff",
            "",
            f"Limits: {PTC['ptc_timeout_seconds']}s timeout, "
            f"{PTC['ptc_max_stdout_bytes'] // 1000}KB stdout cap, "
            f"max {PTC['ptc_max_tool_calls']} tool calls per script.",
            "Print your final result to stdout.",
        ]
    )
    return "\n".join(lines)


class ExecuteCodeTool(BaseTool):
    """Execute a Python script with RPC access to a filtered tool set."""

    name: str = "execute_code"
    description: str = ""
    args_schema: type[BaseModel] = ExecuteCodeInput

    _session_id: str = PrivateAttr(default="")
    _tools_map: dict[str, Any] = PrivateAttr(default_factory=dict)
    _config: Any = PrivateAttr(default=None)

    def _run(
        self,
        code: str,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        return asyncio.run(run_ptc(code, self._tools_map, self._session_id, self._config))

    async def _arun(
        self,
        code: str,
        run_manager: AsyncCallbackManagerForToolRun | None = None,
    ) -> str:
        return await run_ptc(code, self._tools_map, self._session_id, self._config)


def build_ptc_tool(available_tools: list[Any], session_id: str) -> ExecuteCodeTool:
    """Build an ``execute_code`` tool scoped to ``available_tools ∩ ptc_allowed_tools``."""
    allowed = set(PTC["ptc_allowed_tools"])
    tools_map: dict[str, Any] = {
        name: tool
        for tool in available_tools
        if (name := getattr(tool, "name", None)) in allowed and name != "execute_code"
    }
    tool = ExecuteCodeTool()
    tool._session_id = session_id
    tool._tools_map = tools_map
    tool._config = PTC
    tool.description = _build_description(tools_map)
    tool.metadata = {"idempotent": False, "scope": "subagent_only"}
    return tool
