"""LSP precise retrieval — RESEARCHER subagent only (Phase 2 / 2S).

Public surface: the four LangChain tools, the JSON-RPC client, the process-level
server manager, the binary resolver/installer, and the fallback helpers.
"""

from .client import LSPClient
from .fallback import LspAvailability, build_fallback_message, check_lsp_availability
from .installer import install_lsp_server
from .manager import LspServerManager, get_manager
from .protocol import detect_language, format_location, format_symbol, path_to_uri, uri_to_path
from .resolver import (
    get_install_hint,
    get_local_install_hint,
    resolve_lsp_server,
    runtime_dir,
)
from .tools import (
    LspCallHierarchyTool,
    LspFindReferencesTool,
    LspGotoDefinitionTool,
    LspWorkspaceSymbolTool,
    build_lsp_tools,
)

__all__ = [
    "LSPClient",
    "LspAvailability",
    "LspCallHierarchyTool",
    "LspFindReferencesTool",
    "LspGotoDefinitionTool",
    "LspServerManager",
    "LspWorkspaceSymbolTool",
    "build_fallback_message",
    "build_lsp_tools",
    "check_lsp_availability",
    "detect_language",
    "format_location",
    "format_symbol",
    "get_install_hint",
    "get_local_install_hint",
    "get_manager",
    "install_lsp_server",
    "path_to_uri",
    "resolve_lsp_server",
    "runtime_dir",
    "uri_to_path",
]
