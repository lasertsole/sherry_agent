from loguru import logger
from server.trigger.core import app
from server.service import read_sherry_config, write_sherry_config


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
