from loguru import logger
from server.trigger.core import app
from server.service import read_models_config, write_models_config


@app.get("/models-config")
async def read_models_config_handler(request) -> dict:
    """
    Read the project .models (the model configuration file), grouped by prefix.
    Returns: {"groups": [{"name": str, "kind": str, "entries": [{"key", "value"}, ...]}]}
    """
    logger.debug("Reading models config")
    return read_models_config()


@app.put("/models-config")
async def write_models_config_handler(request):
    """
    Update values of known .models keys (values are coerced to their declared
    types; the 200K floor applies to the MAX_TOKEN keys).
    Body: {"changes": {"KEY": "new value", ...}}
    """
    request_json = request.json()

    changes: dict[str, str] = request_json.get("changes", {}) or {}
    if not isinstance(changes, dict):
        return {"success": False, "message": "'changes' must be an object mapping key to value"}

    logger.info(f"Updating models config: keys={list(changes.keys())}")
    try:
        write_models_config(changes)
    except ValueError as e:
        return {"success": False, "message": str(e)}
    logger.info(f"Models config updated: keys={len(changes)}")
    return {"success": True, "message": "Models config updated"}
