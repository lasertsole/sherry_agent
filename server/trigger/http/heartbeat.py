from loguru import logger
from server.trigger.core import app
from server.service import read_heartbeat_file, write_heartbeat_file


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
