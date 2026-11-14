from loguru import logger
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, read_body
from server.service import read_env_file, write_env_file


@app.get("/env")
async def read_env_handler(request) -> dict | tuple[dict, dict, int]:
    """
    Read the project .env file, grouped by prefix.
    Returns: {"groups": [{"name": str, "entries": [{"key", "value"}, ...]}]}
    The 503 path is Robyn's (body, headers, status) tuple.
    """
    logger.debug("Reading environment config")
    try:
        return read_env_file()
    except (FileNotFoundError, OSError) as e:
        # Same install-problem contract as the other config readers.
        logger.error(f".env is unavailable: {e}")
        return {"success": False, "message": str(e)}, {}, 503


@app.put("/env")
async def write_env_handler(request):
    """
    Update values of an already-present .env keys.
    Body: {"changes": {"KEY": "new value", ...}}
    Only keys already present in the file are accepted.
    """
    request_json = read_body(request)
    if request_json is None:
        return bad_request("JSON body required")

    changes: dict[str, str] = request_json.get("changes", {}) or {}
    if not isinstance(changes, dict):
        return {"success": False, "message": "'changes' must be an object mapping key to value"}

    logger.info(f"Updating environment config: keys={list(changes.keys())}")
    try:
        write_env_file(changes)
    except ValueError as e:
        return {"success": False, "message": str(e)}
    except (FileNotFoundError, OSError) as e:
        logger.error(f".env is unavailable for writing: {e}")
        return {"success": False, "message": str(e)}, {}, 503
    logger.info(f"Environment config updated: keys={len(changes)}")
    return {"success": True, "message": "Environment config updated"}
