from loguru import logger
from server.trigger.core import app
from server.service import (
    read_sherry_config,
    read_sherry_list,
    write_sherry_config,
    write_sherry_list,
)


@app.get("/sherry-config")
async def read_sherry_config_handler(request) -> dict:
    """
    Read the project sherry.jsonc application settings (flat entry list).
    Returns: {"entries": [{"key": str, "value": str, "value_edited": bool}, ...]}
    """
    logger.debug("Reading sherry config")
    return read_sherry_config()


@app.put("/sherry-config")
async def write_sherry_config_handler(request):
    """
    Update values of known sherry.jsonc keys (values are coerced to their
    declared types). Body: {"changes": {"KEY": "new value", ...}}
    """
    request_json = request.json()

    changes: dict[str, str] = request_json.get("changes", {}) or {}
    if not isinstance(changes, dict):
        return {"success": False, "message": "'changes' must be an object mapping key to value"}

    logger.info(f"Updating sherry config: keys={list(changes.keys())}")
    try:
        write_sherry_config(changes)
    except ValueError as e:
        return {"success": False, "message": str(e)}
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
        return {"success": False, "message": str(e)}, 400


@app.put("/sherry-config/list/:list_key")
async def write_sherry_list_handler(request, path_params):
    """Replace one list setting. Body: ``{"values": ["gradle", ...]}``.

    Entries are normalized and validated (non-empty, no shell metacharacters,
    bounded) and written as a single-line JSON array, keeping the file's
    comments and ordering.
    """
    list_key = path_params["list_key"]
    request_json = request.json()
    values = request_json.get("values", []) if isinstance(request_json, dict) else None
    if values is None:
        return {"success": False, "message": "'values' is required"}
    logger.info(f"Updating sherry list setting {list_key}: {len(values)} entries")
    try:
        written = write_sherry_list(list_key, values)
    except ValueError as e:
        return {"success": False, "message": str(e)}
    except FileNotFoundError as e:
        return {"success": False, "message": str(e)}
    logger.info(f"Sherry list setting {list_key} updated: {len(written)} entries")
    return {"key": list_key, "values": written}
