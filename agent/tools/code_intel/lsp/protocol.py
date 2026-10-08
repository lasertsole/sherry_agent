"""LSP wire primitives — URIs, positions/ranges, and language detection.

Pure functions only (no process, no IO beyond ``Path``). The tool layer feeds
``line`` / ``character`` as **1-based** values and this module converts them to
LSP-native 0-based positions on the way out, then converts result ranges back to
1-based on the way in.
"""

from __future__ import annotations

import os
from pathlib import Path

from config.features import LSP

__all__ = [
    "detect_language",
    "format_diagnostic",
    "format_location",
    "format_range",
    "format_symbol",
    "format_text_edit",
    "language_id",
    "normalize_workspace_edit",
    "path_to_uri",
    "to_position",
    "uri_to_path",
]

_DIAGNOSTIC_SEVERITY = {1: "error", 2: "warning", 3: "information", 4: "hint"}


def path_to_uri(path: str | os.PathLike[str]) -> str:
    """Return a ``file://`` URI for *path* (absolute, resolved)."""
    return Path(path).resolve().as_uri()


def uri_to_path(uri: str) -> str:
    """Return the filesystem path behind a ``file://`` URI (best effort)."""
    if not uri.startswith("file://"):
        return uri
    from urllib.parse import unquote, urlparse

    parsed = urlparse(uri)
    return unquote(parsed.path)


def detect_language(file_path: str) -> str | None:
    """Map a file path to an LSP language via the configured extensions."""
    suffix = Path(file_path).suffix.lower()
    if not suffix:
        return None
    for language, spec in LSP["lsp_supported_servers"].items():
        if suffix in spec["extensions"]:
            return language
    return None


def language_id(language: str) -> str:
    """Return the LSP ``languageId`` for a configured language key.

    The config key is a server-selection handle (``bash``), while the protocol
    expects the canonical identifier (``shellscript``). Unknown languages fall
    through as-is.
    """
    spec = LSP["lsp_supported_servers"].get(language)
    if spec:
        explicit = spec.get("language_id")
        if explicit:
            return explicit
    return language


def format_diagnostic(diagnostic: dict) -> dict | None:
    """Normalize an LSP ``Diagnostic`` into a compact result dict."""
    if not isinstance(diagnostic, dict):
        return None
    result: dict = {
        "message": diagnostic.get("message"),
        "severity": _DIAGNOSTIC_SEVERITY.get(diagnostic.get("severity"), "unknown"),
        "range": format_range(diagnostic.get("range")),
    }
    for key in ("source", "code"):
        value = diagnostic.get(key)
        if value is not None:
            result[key] = value
    return result


def format_text_edit(edit: dict) -> dict | None:
    """Normalize an LSP ``TextEdit`` (1-based range + replacement text)."""
    if not isinstance(edit, dict):
        return None
    rng = format_range(edit.get("range"))
    if rng is None:
        return None
    return {"range": rng, "new_text": edit.get("newText", "")}


def normalize_workspace_edit(edit: object) -> list[dict]:
    """Flatten a ``WorkspaceEdit`` into ``[{uri, path, edits}]`` groups.

    Only ``TextDocumentEdit`` entries and ``changes`` maps carry text edits; file
    create/rename/delete operations are ignored.
    """
    if not isinstance(edit, dict):
        return []
    grouped: dict[str, list[dict]] = {}
    document_changes = edit.get("documentChanges")
    if isinstance(document_changes, list):
        for change in document_changes:
            if not isinstance(change, dict):
                continue
            document = change.get("textDocument")
            uri = document.get("uri") if isinstance(document, dict) else None
            if not uri:
                continue
            grouped.setdefault(uri, []).extend(_text_edits(change.get("edits")))
    changes = edit.get("changes")
    if isinstance(changes, dict):
        for uri, edits in changes.items():
            if isinstance(uri, str):
                grouped.setdefault(uri, []).extend(_text_edits(edits))
    return [
        {"uri": uri, "path": uri_to_path(uri), "edits": edits}
        for uri, edits in grouped.items()
        if edits
    ]


def _text_edits(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    return [formatted for item in raw if (formatted := format_text_edit(item)) is not None]


def to_position(line: int, character: int) -> dict[str, int]:
    """Convert a 1-based ``(line, character)`` into an LSP 0-based position."""
    return {"line": max(line - 1, 0), "character": max(character - 1, 0)}


def format_range(raw: dict | None) -> dict[str, int] | None:
    if not raw:
        return None
    start = raw.get("start") or {}
    end = raw.get("end") or {}
    return {
        "start_line": int(start.get("line", 0)) + 1,
        "start_character": int(start.get("character", 0)) + 1,
        "end_line": int(end.get("line", 0)) + 1,
        "end_character": int(end.get("character", 0)) + 1,
    }


def format_location(location: dict) -> dict | None:
    """Normalize a ``Location`` or ``LocationLink`` into a compact result dict."""
    if not isinstance(location, dict):
        return None
    uri = location.get("uri") or location.get("targetUri")
    rng = location.get("range") or location.get("targetRange")
    if not uri:
        return None
    return {"uri": uri, "path": uri_to_path(uri), "range": format_range(rng)}


def format_symbol(symbol: dict) -> dict:
    """Normalize a ``WorkspaceSymbol`` / ``SymbolInformation`` entry."""
    result: dict = {"name": symbol.get("name"), "kind": symbol.get("kind")}
    container = symbol.get("containerName")
    if container:
        result["container"] = container
    location = symbol.get("location")
    if isinstance(location, dict):
        result["location"] = format_location(location)
    return result
