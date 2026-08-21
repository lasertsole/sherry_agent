from loguru import logger
from server.trigger.core import app
from server.service import (read_memory_files, write_memory_files)


@app.get("/memory")
async def read_memory_handler(request)-> dict[str, str]:
    """
    Read long-term memory files (workspace/memory/*).
    """
    logger.debug("Reading memory files")

    return read_memory_files()


@app.put("/memory")
async def write_memory_handler(request):
    """
    Write long-term memory files (workspace/memory/*).
    Body: {"file_to_content": {"MEMORY.md": "...", "USER.md": "..."}}
    Only provided files are overwritten; others are left unchanged.
    """
    request_json = request.json()

    file_to_content: dict[str, str] = request_json.get("file_to_content", {})
    file_count = len(file_to_content)
    logger.info(f"Writing memory files: file_count={file_count}, files={list(file_to_content.keys())}")
    write_memory_files(file_to_content)
    logger.info(f"Memory files written: file_count={file_count}")
