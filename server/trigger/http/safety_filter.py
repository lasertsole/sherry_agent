"""The 直播安全 settings tab's backend: keyword rules + the filter log.

    GET  /safety-filter/rules
        -> {"path", "exists", "count", "rules": [{pattern, type, action,
            scope, replacement}, ...]}

        The rules file as the scanner compiled it (mtime-cached), so the tab can
        show the count and an editor seeded with the current content.

    PUT  /safety-filter/rules
        Body {"rules": [...]} -> {"success": true, "count": n}
        or {"success": false, "message": "rule #2: …"} (400-style answer)

        Validation mirrors the scanner's (types/actions/scopes, caps, regex
        compiles); a refused document writes nothing.

    POST /safety-filter/test
        Body {"text": "...", "scope": "input"|"output"}
        -> {"hits": [{pattern, matched_text, action, scope}]}

        The tab's 「测试」 button: which rules a piece of text would hit, without
        filtering anything.

    GET  /safety-filter/log?session_id=<sid>&limit=50
        -> {"events": [...]} (newest first; process-level ring, not persisted)

The master switch and the LLM moderation mode/model are ``sherry.jsonc`` keys
(``safety_filter.*``) saved through /sherry-config — they apply live because the
filter reads that file through an mtime cache.
"""

from loguru import logger

from agent.middlewares.safety_filter import rules_snapshot, scan, validate_rules, write_rules
from agent.middlewares.safety_filter.notify import recent_events
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, ok, read_body


@app.get("/safety-filter/rules")
async def safety_filter_rules_handler(request):
    """Return the loaded keyword rules (the settings tab's snapshot)."""
    return ok(rules_snapshot())


@app.put("/safety-filter/rules")
async def safety_filter_rules_write_handler(request):
    """Validate and persist the keyword rules file."""
    body = read_body(request) or {}
    payload = {"rules": body.get("rules")} if isinstance(body, dict) else {}
    try:
        normalized = validate_rules(payload)
        written = write_rules(payload)
    except ValueError as error:
        return bad_request(str(error))
    except OSError as error:
        logger.warning("safety filter: rules write failed: {}", error)
        return {"success": False, "message": f"cannot write the rules file: {error}"}
    logger.info("safety filter: rules updated ({} rules)", len(written))
    return ok({"count": len(normalized) or len(written)})


@app.post("/safety-filter/test")
async def safety_filter_test_handler(request):
    """Scan one piece of text against the loaded rules (no side effects)."""
    body = read_body(request) or {}
    text = str(body.get("text") or "")
    scope = str(body.get("scope") or "output").strip().lower()
    if scope not in ("input", "output"):
        return bad_request("'scope' must be 'input' or 'output'")
    hits = scan(text, scope=scope)
    return ok(
        {
            "hits": [
                {
                    "pattern": hit.pattern,
                    "matched_text": hit.matched_text,
                    "action": hit.action,
                    "scope": hit.scope,
                }
                for hit in hits
            ]
        }
    )


@app.get("/safety-filter/log")
async def safety_filter_log_handler(request):
    """Recent filter events (newest first) for one session or the whole process."""
    params = request.query_params
    session_id = str(params.get("session_id", "") or "")
    try:
        limit = int(params.get("limit", "50") or 50)
    except (TypeError, ValueError):
        limit = 50
    return ok({"events": recent_events(session_id or None, limit)})
