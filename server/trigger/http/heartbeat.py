from loguru import logger
from server.trigger.core import app
from server.service import read_heartbeat_file, write_heartbeat_file
from server.service.heartbeat_control import get_heartbeat_status, set_heartbeat_enabled


@app.get("/heartbeat")
async def read_heartbeat_handler(request) -> dict[str, str]:
    """
    Read the heartbeat file (workspace/HEARTBEAT.md).
    """
    logger.debug("Reading heartbeat file")

    return read_heartbeat_file()


@app.put("/heartbeat")
async def write_heartbeat_handler(request):
    """
    Write the heartbeat file (workspace/HEARTBEAT.md).
    Body: {"file_to_content": {"HEARTBEAT.md": "..."}}
    Only the provided file is overwritten; others are left unchanged.
    """
    request_json = request.json()

    file_to_content: dict[str, str] = request_json.get("file_to_content", {})
    file_count = len(file_to_content)
    logger.info(
        f"Writing heartbeat file: file_count={file_count}, files={list(file_to_content.keys())}"
    )
    write_heartbeat_file(file_to_content)
    logger.info(f"Heartbeat file written: file_count={file_count}")


@app.get("/heartbeat/status")
async def read_heartbeat_status_handler(request) -> dict:
    """
    Read the global heartbeat switch state.
    Returns: {"enabled": bool, "running": bool, "interval_s": int}
    """
    logger.debug("Reading heartbeat status")

    return get_heartbeat_status()


@app.put("/heartbeat/status")
async def set_heartbeat_status_handler(request):
    """
    Toggle the global heartbeat scheduler (applies immediately, and persists to
    sherry.jsonc so the next boot honours it).
    Body: {"enabled": bool}
    Returns: {"success": bool, "enabled": bool, "running": bool, "interval_s": int}
    """
    request_json = request.json()

    enabled = request_json.get("enabled", None)
    if not isinstance(enabled, bool):
        return {"success": False, "message": "'enabled' must be a boolean"}
    logger.info(f"Setting global heartbeat enabled={enabled}")
    try:
        status = set_heartbeat_enabled(enabled)
    except (ValueError, FileNotFoundError) as e:
        logger.warning(f"Heartbeat toggle rejected: {e}")
        return {"success": False, "message": str(e)}
    return {"success": True, **status}
