from loguru import logger
from server.trigger.core import app
from server.service import read_network_config, write_network_config


@app.get("/network-config")
async def read_network_config_handler(request) -> dict:
    """
    Read the project network.jsonc (network service configuration: the search
    backends' file view, the browser agent's switches, LangSmith tracing),
    grouped by prefix.
    Returns: {"groups": [{"name": str, "kind": str, "entries": [{"key", "value"}, ...]}]}
    """
    logger.debug("Reading network config")
    return read_network_config()


@app.put("/network-config")
async def write_network_config_handler(request):
    """
    Update values of known network.jsonc keys (values are coerced to their
    declared types). Body: {"changes": {"KEY": "new value", ...}}
    The Tavily / SearXNG search keys are written through this same endpoint.
    """
    request_json = request.json()

    changes: dict[str, str] = request_json.get("changes", {}) or {}
    if not isinstance(changes, dict):
        return {"success": False, "message": "'changes' must be an object mapping key to value"}

    logger.info(f"Updating network config: keys={list(changes.keys())}")
    try:
        write_network_config(changes)
    except ValueError as e:
        return {"success": False, "message": str(e)}
    logger.info(f"Network config updated: keys={len(changes)}")
    return {"success": True, "message": "Network config updated"}
