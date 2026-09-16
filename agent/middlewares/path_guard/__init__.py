"""PathGuard — centralized path-argument screening for every tool call.

Defense-in-depth for the per-tool ``resolve_project_path()`` /
``resolve_external_path()`` pattern: a tool that forgets its own path checks
still cannot be driven to read a traversal or hard-denied path. Registered in
the main agent chain right after ``ToolCallNormalize`` (wrap order:
``IterationBudget`` → ``ToolGuardrails`` → ``PathGuard`` → tool), so a rejection
is a normal error ``ToolMessage`` that ToolGuardrails can evaluate like any
other tool failure.

Screening contract (conservative by design):

- only string values under the argument names ``file_path`` / ``path`` /
  ``directory`` / ``dir`` are considered; URL-shaped values (``scheme://``) are
  skipped so non-path semantics (e.g. a URL passed as ``path``) are never
  misread as filesystem paths;
- ``..`` traversal components (URL-decoded, backslash-normalized — the shared
  ``has_traversal_component`` predicate) are rejected;
- a value that ``resolve_project_path()`` accepts is passed through untouched;
- a value that resolves outside ``ROOT_DIR`` is passed through **unless** it is
  on the hard-deny floor (the YOLO deny list / system credential files). Any
  other external path is left to the tool's own ``resolve_external_path()``
  HITL flow — intercepting it here would approve (or block) it twice, since
  the tool re-runs the same gate during execution;
- missing/unresolvable targets and unknown exception classes fall through to
  the tool, which owns its own error surface.

The middleware never rewrites arguments and never triggers an approval
interrupt itself; the only approval flow remains the one inside the tools.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from loguru import logger
from typing import override

from config.path import ROOT_DIR
from pub.func.path import has_traversal_component


@dataclass(frozen=True, slots=True)
class _Rejection:
    """A screened path argument that must not reach the tool."""

    key: str
    value: str
    reason: str


_PATH_ARG_KEYS = frozenset({"file_path", "path", "directory", "dir"})

#: System credential files that are never a legitimate project read; a HITL
#: prompt for them is at best noise. Mirrors the terminal sensitive-file set.
_SYSTEM_DENY_PATHS: tuple[str, ...] = ("/etc/passwd", "/etc/shadow", "/etc/sudoers")


def _system_deny_paths() -> tuple[Path, ...]:
    return tuple(Path(path).resolve() for path in _SYSTEM_DENY_PATHS)


def _external_target(value: str) -> Path | None:
    """Resolve value as an external-path candidate; None when unresolvable."""
    try:
        target = Path(os.path.expanduser(value))
        if not target.is_absolute():
            target = ROOT_DIR / target
        return target.resolve()
    except (OSError, RuntimeError):
        return None


def _is_hard_denied(target: Path) -> bool:
    """Hard-deny floor: the YOLO deny list plus the system credential files."""
    if any(target == denied for denied in _system_deny_paths()):
        return True
    from agent.tools.pub_base.path_utils import _is_yolo_denied

    return _is_yolo_denied(target)


def _screen_path_arg(value: str) -> str | None:
    """Return a rejection reason for value, or None to let the call proceed."""
    if has_traversal_component(value):
        return "path traversal components are not allowed"
    from agent.tools.pub_base.path_utils import PathOutOfBoundsError, resolve_project_path

    try:
        resolve_project_path(value)
    except PathOutOfBoundsError:
        target = _external_target(value)
        if target is not None and _is_hard_denied(target):
            return "path is on the hard deny list (credentials / system files)"
    except (OSError, RuntimeError):
        return None
    return None


class PathGuard(AgentMiddleware):
    """Reject tool calls whose path arguments are definitively out of bounds."""

    def _find_rejection(self, request: ToolCallRequest) -> _Rejection | None:
        args: Any = request.tool_call.get("args")
        if not isinstance(args, dict):
            return None
        for key in sorted(_PATH_ARG_KEYS):
            value = args.get(key)
            if not isinstance(value, str):
                continue
            candidate = value.strip()
            if not candidate or "://" in candidate:
                continue
            reason = _screen_path_arg(candidate)
            if reason is not None:
                return _Rejection(key=key, value=value, reason=reason)
        return None

    @staticmethod
    def _blocked_message(request: ToolCallRequest, rejection: _Rejection) -> ToolMessage:
        tool_name = request.tool_call.get("name", "unknown")
        logger.warning(
            "PathGuard blocked {}.{}={!r}: {}",
            tool_name,
            rejection.key,
            rejection.value,
            rejection.reason,
        )
        return ToolMessage(
            content=(
                f"PathGuard: rejected {tool_name}({rejection.key}={rejection.value!r}): "
                f"{rejection.reason}. Use a path inside the project root; external paths "
                "go through the file tools' human-approval flow."
            ),
            tool_call_id=request.tool_call.get("id", ""),
            name=tool_name,
            status="error",
        )

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        logger.debug("{} wrap_tool_call hook fired", type(self).__name__)
        rejection = self._find_rejection(request)
        if rejection is not None:
            return self._blocked_message(request, rejection)
        return handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage]],
    ) -> ToolMessage:
        logger.debug("{} awrap_tool_call hook fired", type(self).__name__)
        rejection = self._find_rejection(request)
        if rejection is not None:
            return self._blocked_message(request, rejection)
        return await handler(request)
