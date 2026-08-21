from loguru import logger
from server.trigger.core import app
from server.service import read_env_file, write_env_file


@app.get("/env")
async def read_env_handler(request) -> dict:
    """
    Read the project .env file, grouped by prefix.
    Returns: {"groups": [{"name": str, "entries": [{"key", "value"}, ...]}]}
    """
    logger.debug("Reading environment config")
    return read_env_file()


@app.put("/env")
async def write_env_handler(request):
    """
    Update values of an already-present .env keys.
    Body: {"changes": {"KEY": "new value", ...}}
    Only keys already present in the file are accepted.
    """
    request_json = request.json()

    changes: dict[str, str] = request_json.get("changes", {}) or {}
    if not isinstance(changes, dict):
        return {"success": False, "message": "'changes' must be an object mapping key to value"}

    logger.info(f"Updating environment config: keys={list(changes.keys())}")
    write_env_file(changes)
    logger.info(f"Environment config updated: keys={len(changes)}")
    return {"success": True, "message": "Environment config updated"}
