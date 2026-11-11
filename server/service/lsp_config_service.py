"""LSP configuration service: merged read, project edit/upload, server test.

The three configuration layers live in ``agent/tools/code_intel/lsp/config_merge.py``
(builtin code table → skill ``lsp.json`` → ``<project>/.sherry/lsp.json``). This
service is the HTTP face: it resolves the session's project root (the same root
the LSP tools acquire servers in), validates every write with the same merge
validator, and can probe one declaration by resolving its binary and completing
a real ``initialize`` handshake.

Security: a declaration's ``command[0]`` is a bare executable name validated by
``config_merge``; the actual binary always comes from the five-tier resolver,
never from the request. Writes are atomic and fail closed on validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from agent.tools.code_intel.lsp.client import LSPClient
from agent.tools.code_intel.lsp.config_merge import (
    MAX_FILE_BYTES,
    effective_lsp_servers,
    project_config_path,
    project_payload,
    read_project_config,
    write_project_config,
)
from agent.tools.code_intel.lsp.protocol import language_id
from agent.tools.code_intel.lsp.resolver import get_install_hint, resolve_lsp_server
from config.features import LSP
from config.path import resolve_default_project_dir
from runtime.session.project_dir import read_project_dir_durable

__all__ = [
    "delete_language",
    "get_config",
    "get_project",
    "put_project",
    "session_root",
    "test_server",
    "upload_project",
]


def session_root(session_id: str) -> Path:
    """The session's effective project root (never ``None``)."""
    return read_project_dir_durable(session_id) or resolve_default_project_dir()


def _config_entry(language: str, spec: dict[str, Any], view, root: Path) -> dict[str, Any]:
    binary = resolve_lsp_server(language, str(root), view.servers)
    return {
        "language": language,
        "command": list(spec.get("command") or []),
        "extensions": list(spec.get("extensions") or []),
        "language_id": language_id(language, view.servers),
        "install_hint": spec.get("install_hint") or get_install_hint(language, view.servers),
        "local_install": spec.get("local_install"),
        "source": view.source_of(language),
        "enabled": language in view.enabled,
        "installed": bool(binary),
        "binary_path": binary or "",
    }


def get_config(session_id: str) -> dict[str, Any]:
    """Effective merged configuration + the raw project layer for the editor."""
    root = session_root(session_id)
    view = effective_lsp_servers(root)
    servers = [
        _config_entry(language, spec, view, root) for language, spec in sorted(view.servers.items())
    ]
    payload = project_payload(root)
    return {
        "success": True,
        "session_id": session_id,
        "root": str(root),
        "project_path": str(project_config_path(root)),
        "servers": servers,
        "enabled": view.enabled,
        "project": payload,
        "errors": view.errors,
    }


def get_project(session_id: str) -> dict[str, Any]:
    """The project layer only (what the editable table/upload starts from)."""
    root = session_root(session_id)
    servers, enable, errors = read_project_config(root)
    return {
        "success": True,
        "session_id": session_id,
        "root": str(root),
        "path": str(project_config_path(root)),
        "exists": project_config_path(root).is_file(),
        "servers": servers,
        "enable": enable,
        "errors": errors,
    }


def put_project(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and atomically replace the project layer."""
    root = session_root(session_id)
    view, errors = write_project_config(root, payload)
    if view is None:
        return {"success": False, "message": "; ".join(errors) or "invalid configuration"}
    return {
        "success": True,
        "session_id": session_id,
        "root": str(root),
        "path": str(project_config_path(root)),
        "servers": len(payload.get("servers") or {}),
    }


def upload_project(session_id: str, content: object) -> dict[str, Any]:
    """Validate an uploaded lsp.json text and store the project layer.

    The client reads the chosen file and sends its TEXT in ``content``: the
    server never trusts a client-supplied path and applies the same validator
    as a manual save.
    """
    text = content if isinstance(content, str) else ""
    if not text.strip():
        return {"success": False, "message": "the uploaded file is empty"}
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        return {"success": False, "message": f"lsp.json exceeds {MAX_FILE_BYTES} bytes"}
    try:
        payload = json.loads(text)
    except ValueError as exc:
        return {"success": False, "message": f"invalid JSON: {exc}"}
    if not isinstance(payload, dict):
        return {"success": False, "message": "lsp.json must contain a JSON object"}
    return put_project(session_id, payload)


def delete_language(session_id: str, language: str) -> dict[str, Any]:
    """Remove one language from the project layer (never touches builtin/skill)."""
    language = str(language or "").strip()
    if not language:
        return {"success": False, "message": "language is required"}
    root = session_root(session_id)
    servers, enable, _errors = read_project_config(root)
    if language not in servers:
        return {"success": False, "message": f"{language!r} is not a project-level entry"}
    server_payload = dict(servers[language])
    del servers[language]
    return payload_result(
        put_project(
            session_id,
            {"servers": servers, "enable": [item for item in enable if item != language]},
        ),
        language,
        server_payload,
    )


def payload_result(result: dict[str, Any], language: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Annotate a successful delete with what was removed (for the UI toast)."""
    if result.get("success"):
        result["deleted"] = language
        result["removed"] = spec
    return result


def test_server(session_id: str, language: str) -> dict[str, Any]:
    """Resolve one declaration and complete a real initialize handshake.

    This is a one-shot probe: its own ``LSPClient`` is shut down immediately, so
    the manager's shared pool is untouched. A missing binary / timeout / server
    error is reported as ``ok: false`` — it never raises.
    """
    language = str(language or "").strip()
    if not language:
        return {"success": False, "ok": False, "message": "language is required"}
    root = session_root(session_id)
    view = effective_lsp_servers(root)
    spec = view.servers.get(language)
    if spec is None:
        return {"success": False, "ok": False, "message": f"no server declaration for {language!r}"}
    binary = resolve_lsp_server(language, str(root), view.servers)
    if not binary:
        return {
            "success": True,
            "ok": False,
            "language": language,
            "installed": False,
            "message": get_install_hint(language, view.servers),
        }
    client = LSPClient(
        language,
        str(root),
        [binary, *list(spec.get("command") or [])[1:]],
        request_timeout_s=float(LSP["lsp_request_timeout_s"]),
        start_timeout_s=float(LSP["lsp_server_start_timeout_s"]),
        max_file_bytes=int(LSP["lsp_max_file_bytes"]),
        max_opened_files=int(LSP["lsp_max_opened_files"]),
    )
    try:
        try:
            error = client.start()
        except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
            error = str(exc)
    finally:
        try:
            client.shutdown()
        except Exception:  # noqa: BLE001 - teardown must never hide the verdict
            client.force_kill()
    if error:
        logger.info("LSP config test failed: language={} error={}", language, error)
        return {
            "success": True,
            "ok": False,
            "language": language,
            "installed": True,
            "binary": binary,
            "message": error,
        }
    return {
        "success": True,
        "ok": True,
        "language": language,
        "installed": True,
        "binary": binary,
        "message": f"{language_id(language, view.servers)} initialized",
    }
