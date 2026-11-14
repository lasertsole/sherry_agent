from loguru import logger
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, read_body
from server.service import (
    clear_all_model_caps,
    clear_model_cap,
    read_model_caps,
    read_model_roles,
    read_sherry_config,
    read_sherry_list,
    write_sherry_config,
    write_sherry_list,
)


@app.get("/sherry-config")
async def read_sherry_config_handler(request):
    """
    Read the project sherry.jsonc application settings (flat entry list).
    Returns: {"entries": [{"key": str, "value": str, "value_edited": bool}, ...]}
    """
    logger.debug("Reading sherry config")
    try:
        return read_sherry_config()
    except (FileNotFoundError, OSError) as e:
        # A missing/unreadable file is an INSTALL problem, not a server bug:
        # answer a structured 503 instead of letting the exception become a
        # bare HTTP 500 (the packaged app's first run used to surface one).
        logger.error(f"sherry.jsonc is unavailable: {e}")
        return {"success": False, "message": str(e)}, {}, 503


@app.get("/sherry-config/model-roles")
async def read_model_roles_handler(request):
    """
    Read the effective model of every judge / subagent role, with its source.

    Answers ``{"roles": [{"role", "family", "configured", "local", "source",
    "effective_provider", "effective_model", "max_output_tokens"}, ...]}``. A
    role family whose provider / api_base is empty reports the family it
    inherits from (auxiliary / main / depth) instead of a misleading blank.
    """
    return read_model_roles()


@app.put("/sherry-config")
async def write_sherry_config_handler(request):
    """
    Update values of known sherry.jsonc keys (values are coerced to their
    declared types). Body: {"changes": {"KEY": "new value", ...}}
    """
    request_json = read_body(request)
    if request_json is None:
        return bad_request("JSON body required")

    changes: dict[str, str] = request_json.get("changes", {}) or {}
    if not isinstance(changes, dict):
        return {"success": False, "message": "'changes' must be an object mapping key to value"}

    logger.info(f"Updating sherry config: keys={list(changes.keys())}")
    try:
        write_sherry_config(changes)
    except ValueError as e:
        return {"success": False, "message": str(e)}
    except (FileNotFoundError, OSError) as e:
        # The file was never seeded and the seed failed too: an INSTALL
        # problem, not a server bug — same structured 503 as the read path.
        logger.error(f"sherry.jsonc is unavailable for writing: {e}")
        return {"success": False, "message": str(e)}, {}, 503
    logger.info(f"Sherry config updated: keys={len(changes)}")
    return {"success": True, "message": "Sherry config updated"}


@app.get("/sherry-config/list/:list_key")
async def read_sherry_list_handler(request, path_params):
    """Read one list setting (``regression_gate_allowed_executables`` / ``yolo_deny_paths``).

    Lists are outside the scalar registry above: the code owns the defaults and
    the user's entries are MERGED at runtime, so this endpoint returns only the
    user's own list (an empty list means "defaults only"). Unknown keys are
    refused so the UI cannot author settings nothing consumes.
    """
    list_key = path_params["list_key"]
    try:
        return {"key": list_key, "values": read_sherry_list(list_key)}
    except ValueError as e:
        return {"success": False, "message": str(e)}, {}, 400


@app.put("/sherry-config/list/:list_key")
async def write_sherry_list_handler(request, path_params):
    """Replace one list setting. Body: ``{"values": ["gradle", ...]}``.

    Entries are normalized and validated (non-empty, no shell metacharacters,
    bounded) and written as a single-line JSON array, keeping the file's
    comments and ordering.
    """
    list_key = path_params["list_key"]
    request_json = read_body(request)
    if request_json is None:
        return bad_request("JSON body required")
    values = request_json.get("values", []) if isinstance(request_json, dict) else None
    if values is None:
        return bad_request("'values' is required")
    logger.info(f"Updating sherry list setting {list_key}: {len(values)} entries")
    try:
        written = write_sherry_list(list_key, values)
    except ValueError as e:
        return {"success": False, "message": str(e)}
    except FileNotFoundError as e:
        return {"success": False, "message": str(e)}
    logger.info(f"Sherry list setting {list_key} updated: {len(written)} entries")
    return {"key": list_key, "values": written}


@app.get("/sherry-config/model-caps")
async def read_model_caps_handler(request):
    """List the persisted structured-output degradations.

    Returns ``{"entries": [{provider, model, level, effective_level, reason,
    escalated_at, updated_at, reprobe_count}]}``. ``level`` is what is stored;
    ``effective_level`` is what the next spawn would use after the re-probe
    decay. The list is empty when no model has degraded.
    """
    try:
        return {"entries": read_model_caps()}
    except Exception as e:  # noqa: BLE001 - a read failure is a structured error
        logger.warning("Reading model caps failed: {}", e)
        return {"success": False, "message": str(e)}, {}, 500


@app.delete("/sherry-config/model-caps")
async def clear_model_caps_handler(request):
    """Clear one degradation (``?provider=&model=``) or every one (no query).

    Model names may contain ``/``; this query form is the one the client uses
    for them. The path form below is kept for a simple provider/model pair.
    """
    provider = str(request.query_params.get("provider", "") or "").strip()
    model = str(request.query_params.get("model", "") or "").strip()
    if provider or model:
        if not provider or not model:
            return (
                {
                    "success": False,
                    "message": "provider and model are required together",
                },
                {},
                400,
            )
        deleted = 1 if clear_model_cap(provider, model) else 0
    else:
        deleted = clear_all_model_caps()
    logger.info(f"Cleared model caps: {deleted} entry/entries")
    return {"success": True, "deleted": deleted}


@app.delete("/sherry-config/model-caps/:provider/:model")
async def clear_model_cap_handler(request, path_params):
    """Clear one degradation by path segments (slash-free model names)."""
    provider = str(path_params.get("provider") or "").strip()
    model = str(path_params.get("model") or "").strip()
    if not provider or not model:
        return {"success": False, "message": "provider and model are required"}, {}, 400
    deleted = 1 if clear_model_cap(provider, model) else 0
    logger.info(f"Cleared model cap {provider}/{model}: {deleted}")
    return {"success": True, "deleted": deleted}
