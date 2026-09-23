"""LSP fallback chain — graceful degradation when a language server is unavailable.

Resolution order the caller should present:

  1. LSP available → use the precise LSP tools (goto_definition, find_references, ...)
  2. not_installed → return the install hint and point at ``explore`` (Phase 1)
  3. not_configured / no match → point at ``terminal`` (rg/grep)

Every message ends with an actionable next step; nothing here starts a process
or raises.
"""

from __future__ import annotations

from typing import Literal

from config.features import LSP

from .resolver import get_install_hint, get_local_install_hint, resolve_lsp_server

__all__ = ["LspAvailability", "build_fallback_message", "check_lsp_availability"]

type LspAvailability = Literal["available", "not_installed", "not_configured"]

_FALLBACK_TOOLS = "`explore` (tree-sitter symbol index) or `terminal` (rg/grep)"


def check_lsp_availability(language: str, cwd: str | None = None) -> tuple[LspAvailability, str]:
    """Check whether an LSP server is available for *language*.

    Returns ``(status, message)`` where an ``available`` message is the resolved
    binary path and the other two carry an actionable hint/explanation.
    """
    if language not in LSP["lsp_enabled_languages"]:
        enabled = ", ".join(LSP["lsp_enabled_languages"])
        return "not_configured", f"Language '{language}' is not in the enabled list: {enabled}"

    binary = resolve_lsp_server(language, cwd)
    if binary:
        return "available", binary

    hint = get_install_hint(language)
    local_hint = get_local_install_hint(language)
    if local_hint:
        hint = f"{hint} (project-local: {local_hint})"
    return "not_installed", hint


def build_fallback_message(
    language: str,
    requested_tool: str,
    availability: LspAvailability,
    message: str,
) -> str:
    """Build the user-facing message returned when an LSP tool cannot run."""
    if availability == "not_configured":
        return f"{requested_tool}: {message}. Fallback: use {_FALLBACK_TOOLS} for now."
    if availability == "not_installed":
        return (
            f"{requested_tool}: LSP server for '{language}' is not installed. "
            f"Install it with: {message}. "
            f"Fallback: use {_FALLBACK_TOOLS} for now."
        )
    return ""
