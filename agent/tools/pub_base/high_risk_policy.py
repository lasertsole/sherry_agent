"""Metadata policy for the high-risk guard.

Two independent predicates derive from tool metadata (never from a tool-name
list, so a new tool declares its own posture):

* :func:`is_high_risk` — the tool can cause an irreversible external effect.
  Explicit ``metadata["high_risk"]`` wins; an ``mcp__`` tool without the key
  defaults to ``True`` (an external server's side effects are unknowable), and
  every other unannotated tool defaults to ``False`` so the ordinary workflow
  (read / write / plan) is never ledgered.
* :func:`should_scan_keywords` — the call's arguments may carry a dangerous
  operation. ``metadata["scan_keywords"] = True`` covers ``terminal`` /
  ``python_repl`` / PTC; every high-risk tool is scanned as well.

The operation ledger only tracks tools that are high-risk AND not idempotent:
re-running a deploy/charge is the risk the ledger exists for, while re-running
``write_file`` is normal work.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "is_high_risk",
    "is_idempotent",
    "should_ledger",
    "should_scan_keywords",
    "should_scan_keywords_by_name",
    "MCP_PREFIX",
]

MCP_PREFIX = "mcp__"

#: Tool names whose arguments are scanned even though the tool itself is not
#: ledgered: the shell/interpreter text decides the risk, not the tool.
_SCANNED_TOOL_NAMES = frozenset({"terminal", "python_repl", "execute_code"})


def _metadata(tool: Any) -> dict[str, Any]:
    metadata = getattr(tool, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _tool_name(tool: Any) -> str:
    name = getattr(tool, "name", "")
    return str(name or "")


def is_high_risk(tool: Any) -> bool:
    """Whether *tool* is high-risk (explicit metadata, else MCP fail-safe)."""
    metadata = _metadata(tool)
    if "high_risk" in metadata:
        return bool(metadata["high_risk"])
    return _tool_name(tool).startswith(MCP_PREFIX)


def is_idempotent(tool: Any) -> bool:
    """Whether repeating this exact call is harmless (metadata opt-in)."""
    return _metadata(tool).get("idempotent") is True


def should_ledger(tool: Any) -> bool:
    """Whether an execution goes into the high-risk operation ledger."""
    return is_high_risk(tool) and not is_idempotent(tool)


def should_scan_keywords(tool: Any) -> bool:
    """Whether the call arguments are scanned for high-risk keywords."""
    if is_high_risk(tool):
        return True
    if _metadata(tool).get("scan_keywords") is True:
        return True
    return _tool_name(tool) in _SCANNED_TOOL_NAMES


def should_scan_keywords_by_name(tool_name: str) -> bool:
    """Name-only variant for call sites that received a tool CALL, not a tool.

    The HITL handler routes a ``tool_call`` dict and has no tool object; this
    mirrors :func:`should_scan_keywords` for the names that always scan (an MCP
    call is recognised by its prefix).
    """
    name = str(tool_name or "")
    return name.startswith(MCP_PREFIX) or name in _SCANNED_TOOL_NAMES
