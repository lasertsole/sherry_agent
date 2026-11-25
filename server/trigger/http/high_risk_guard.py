"""High-risk guard panel API: keyword registry + executed-operation ledger.

    GET    /sessions/:sid/high-risk-keywords
        -> {"keywords": [...], "ops": [...]}   (one panel load)
    POST   /sessions/:sid/high-risk-keywords
        body {"keyword": "...", "scope": "session"|"global"}
    PUT    /sessions/:sid/high-risk-keywords
        body {"keyword": "...", "policy": "block"|"allow"}
        or  {"keyword_id": 12, "policy": "allow"}
    DELETE /sessions/:sid/high-risk-keywords/:keyword_id
    POST   /sessions/:sid/high-risk-keywords/reset
        body {"keyword": "..."}   (clear the executed marker -> one more run)
    DELETE /sessions/:sid/high-risk-ops/:op_id
    GET    /high-risk-keywords/builtin

The same store backs the ``high_risk_guard`` tool and the ToolGuardrails scan;
the panel is just the human view. All writes validate keywords (>= 3 chars).

Transport gate: the ledger's args_preview can be sensitive, so every route here
requires a same-host or HTTPS/WSS transport (see ``transport_guard``). An
unsafe transport gets 200 + empty data + a degraded notice, never real data.
"""

from loguru import logger

from agent.tools.pub_base.high_risk_guard_store import default_store, validate_keyword
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, not_found, ok, read_body, to_text_response
from server.trigger.http.transport_guard import degraded_response, ledger_transport_allowed


def _degraded(request) -> object | None:
    """A 200+empty-data response when the transport cannot carry ledger data.

    Same-host and encrypted transports pass. Everything else gets an empty
    payload plus the notice — never the real keywords/ops (which may carry a
    sensitive ``args_preview``). Writes are refused the same way so a plaintext
    remote caller cannot mutate the ledger either.
    """
    if ledger_transport_allowed(request):
        return None
    return to_text_response(200, degraded_response())


def _session_id(path_params) -> str:
    return str((path_params or {}).get("session_id") or "").strip()


def _keyword_id(path_params) -> int:
    return int((path_params or {}).get("keyword_id") or 0)


@app.get("/high-risk-keywords/builtin")
async def high_risk_builtin_keywords_handler(request):
    """The read-only built-in keyword list (the panel's reference chips)."""
    if (degraded := _degraded(request)) is not None:
        return degraded
    from config.features import HIGH_RISK_GUARD

    return ok({"keywords": list(HIGH_RISK_GUARD["builtin_keywords"])})


@app.get("/sessions/:session_id/high-risk-keywords")
async def high_risk_keywords_list_handler(request, path_params):
    """Everything the panel renders: keywords + recorded operations."""
    if (degraded := _degraded(request)) is not None:
        return degraded
    session_id = _session_id(path_params)
    if not session_id:
        return bad_request("session_id is required")
    store = default_store()
    return ok(
        {
            "keywords": store.list_keywords(session_id),
            "ops": store.list_ops(session_id),
        }
    )


@app.post("/sessions/:session_id/high-risk-keywords")
async def high_risk_keywords_add_handler(request, path_params):
    """Register a keyword for this session (or globally)."""
    if (degraded := _degraded(request)) is not None:
        return degraded
    session_id = _session_id(path_params)
    body = read_body(request) or {}
    keyword = str(body.get("keyword") or "")
    error = validate_keyword(keyword)
    if error is not None:
        return bad_request(error)
    scope = "global" if str(body.get("scope") or "").strip().lower() == "global" else "session"
    ok_added, message = default_store().add_keyword(session_id, keyword, source="user", scope=scope)
    if not ok_added:
        return bad_request(message)
    logger.info("high-risk guard: keyword added ({}): {}", scope, message)
    return ok({"success": True, "message": message})


@app.put("/sessions/:session_id/high-risk-keywords")
async def high_risk_keywords_policy_handler(request, path_params):
    """Switch one keyword between block and allow."""
    if (degraded := _degraded(request)) is not None:
        return degraded
    session_id = _session_id(path_params)
    body = read_body(request) or {}
    keyword = str(body.get("keyword") or "")
    policy = str(body.get("policy") or "").strip().lower()
    if not keyword:
        return bad_request("keyword is required")
    if policy not in {"block", "allow"}:
        return bad_request("policy must be 'block' or 'allow'")
    ok_set, message = default_store().set_policy(session_id, keyword, policy)
    if not ok_set:
        return not_found(message)
    return ok({"success": True, "message": message})


@app.delete("/sessions/:session_id/high-risk-keywords/:keyword_id")
async def high_risk_keywords_delete_handler(request, path_params):
    """Delete one keyword row (a built-in override returns to its default)."""
    if (degraded := _degraded(request)) is not None:
        return degraded
    session_id = _session_id(path_params)
    ok_deleted, message = default_store().remove_keyword(session_id, _keyword_id(path_params))
    if not ok_deleted:
        return not_found(message)
    return ok({"success": True, "message": message})


@app.post("/sessions/:session_id/high-risk-keywords/reset")
async def high_risk_keywords_reset_handler(request, path_params):
    """Clear a keyword's executed marker so the operation may run once more."""
    if (degraded := _degraded(request)) is not None:
        return degraded
    session_id = _session_id(path_params)
    body = read_body(request) or {}
    keyword = str(body.get("keyword") or "").strip()
    if not keyword:
        return bad_request("keyword is required")
    if not default_store().reset_executed(session_id, keyword):
        return not_found(f"keyword '{keyword}' has no executed marker")
    return ok({"success": True})


@app.delete("/sessions/:session_id/high-risk-ops/:op_id")
async def high_risk_ops_delete_handler(request, path_params):
    """Delete one ledger row (the user's explicit re-run)."""
    if (degraded := _degraded(request)) is not None:
        return degraded
    session_id = _session_id(path_params)
    op_id = int((path_params or {}).get("op_id") or 0)
    if not default_store().remove_op(session_id, op_id):
        return not_found(f"unknown operation id: {op_id}")
    return ok({"success": True})
