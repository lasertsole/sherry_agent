"""LSP configuration HTTP face (系统配置 → LSP tab).

    GET    /lsp/config?session_id=…            merged builtin+skill+project view
    GET    /lsp/config/project?session_id=…    the project layer for the editor
    PUT    /lsp/config/project                 {session_id, servers, enable}
    POST   /lsp/config/project                 {session_id, content} (uploaded JSON text)
    DELETE /lsp/config/project/language        {session_id, language}
    POST   /lsp/config/test                    {session_id, language} → real initialize

The session id resolves the project root exactly like the LSP tools do, so the
file edited here is the file those tools merge. Every write is validated by
``config_merge`` (bare command[0], shell-free argv, dot extensions, bounded
sizes) and persisted atomically; a refused write leaves the previous file.
"""

from __future__ import annotations

import asyncio

from server.service import lsp_config_service
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, ok, read_body


def _session_id(payload: object, *, query: bool = False) -> str:
    if query:
        return str(getattr(payload, "query_params", {}).get("session_id", "") or "")
    return str((payload or {}).get("session_id", "") or "") if isinstance(payload, dict) else ""


@app.get("/lsp/config")
async def lsp_config_get_handler(request):
    session_id = _session_id(request, query=True)
    if not session_id:
        return bad_request("session_id is required")
    return ok(await asyncio.to_thread(lsp_config_service.get_config, session_id))


@app.get("/lsp/config/project")
async def lsp_project_get_handler(request):
    session_id = _session_id(request, query=True)
    if not session_id:
        return bad_request("session_id is required")
    return ok(await asyncio.to_thread(lsp_config_service.get_project, session_id))


@app.put("/lsp/config/project")
async def lsp_project_put_handler(request):
    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    session_id = _session_id(body)
    if not session_id:
        return bad_request("session_id is required")
    payload = {"servers": body.get("servers", {}), "enable": body.get("enable", [])}
    result = await asyncio.to_thread(lsp_config_service.put_project, session_id, payload)
    return ok(result) if result.get("success") else bad_request(str(result.get("message")))


@app.post("/lsp/config/project")
async def lsp_project_upload_handler(request):
    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    session_id = _session_id(body)
    if not session_id:
        return bad_request("session_id is required")
    result = await asyncio.to_thread(
        lsp_config_service.upload_project, session_id, body.get("content")
    )
    return ok(result) if result.get("success") else bad_request(str(result.get("message")))


@app.delete("/lsp/config/project/language")
async def lsp_project_delete_language_handler(request):
    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    session_id = _session_id(body)
    if not session_id:
        return bad_request("session_id is required")
    result = await asyncio.to_thread(
        lsp_config_service.delete_language, session_id, str(body.get("language") or "")
    )
    return ok(result) if result.get("success") else bad_request(str(result.get("message")))


@app.post("/lsp/config/test")
async def lsp_config_test_handler(request):
    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    session_id = _session_id(body)
    if not session_id:
        return bad_request("session_id is required")
    language = str(body.get("language", "") or "")
    if not language:
        return bad_request("language is required")
    return ok(await asyncio.to_thread(lsp_config_service.test_server, session_id, language))
