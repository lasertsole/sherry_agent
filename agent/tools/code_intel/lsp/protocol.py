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
    "format_location",
    "format_range",
    "format_symbol",
    "path_to_uri",
    "to_position",
    "uri_to_path",
]


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
