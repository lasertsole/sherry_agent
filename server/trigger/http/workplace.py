from server.trigger.core import app
from loguru import logger
from server.service import (read_system_prompt_file, write_system_prompt_file, update_system_prompt_file)

@app.get("/system_prompt")
async def read_system_prompt_handler(request)-> dict[str, str]:
    """
    Read system prompt files
    """
    logger.debug("Reading system prompt")

    return read_system_prompt_file()


@app.put("/system_prompt")
async def write_system_prompt_file_handler(request):
    """
    Write system prompt files
    """
    request_json = request.json()

    file_to_content: dict[str, str] = request_json.get("file_to_content", {})
    file_count = len(file_to_content)
    logger.info(f"Writing system prompt: file_count={file_count}, files={list(file_to_content.keys())}")
    write_system_prompt_file(file_to_content)
    logger.info(f"System prompt written: file_count={file_count}")


@app.patch("/system_prompt")
async def update_system_prompt_file_handler(request):
    """
    Update system prompt files
    """
    request_json = request.json()

    file_to_content: dict[str, str] = request_json.get("file_to_content", {})
    file_count = len(file_to_content)
    logger.info(f"Updating system prompt: file_count={file_count}, files={list(file_to_content.keys())}")
    update_system_prompt_file(file_to_content)
    logger.info(f"System prompt updated: file_count={file_count}")
