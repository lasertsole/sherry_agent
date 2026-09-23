"""LSP precise retrieval — RESEARCHER subagent only (Phase 2 / 2S / 2X).

Public surface: the eight LangChain tools, the JSON-RPC client, the process-level
server manager, the binary resolver/installer, and the fallback helpers.
"""

from .client import LSPClient
from .fallback import LspAvailability, build_fallback_message, check_lsp_availability
from .installer import install_lsp_server
from .manager import LspServerManager, get_manager
from .protocol import (
    detect_language,
    format_diagnostic,
    format_location,
    format_symbol,
    format_text_edit,
    language_id,
    normalize_workspace_edit,
    path_to_uri,
    uri_to_path,
)
from .resolver import (
    get_install_hint,
    get_local_install_hint,
    resolve_lsp_server,
    runtime_dir,
)
from .tools import (
    LspCallHierarchyTool,
    LspDiagnosticsTool,
    LspFindReferencesTool,
    LspFormatTool,
    LspGotoDefinitionTool,
    LspRenameTool,
    LspStatusTool,
    LspWorkspaceSymbolTool,
    build_lsp_tools,
)

__all__ = [
    "LSPClient",
    "LspAvailability",
    "LspCallHierarchyTool",
    "LspDiagnosticsTool",
    "LspFindReferencesTool",
    "LspFormatTool",
    "LspGotoDefinitionTool",
    "LspRenameTool",
    "LspServerManager",
    "LspStatusTool",
    "LspWorkspaceSymbolTool",
    "build_fallback_message",
    "build_lsp_tools",
    "check_lsp_availability",
    "detect_language",
    "format_diagnostic",
    "format_location",
    "format_symbol",
    "format_text_edit",
    "get_install_hint",
    "get_local_install_hint",
    "get_manager",
    "install_lsp_server",
    "language_id",
    "normalize_workspace_edit",
    "path_to_uri",
    "resolve_lsp_server",
    "runtime_dir",
    "uri_to_path",
]
