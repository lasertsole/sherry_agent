from .core import app
from loguru import logger
from server.service import (read_system_prompt_file, write_system_prompt_file, update_system_prompt_file, read_character,
                            write_character, update_character)

@app.get("/system_prompt")
async def read_system_prompt_handler(request):
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
    result = write_system_prompt_file(file_to_content)
    logger.info(f"System prompt written: file_count={file_count}")

    return result


@app.patch("/system_prompt")
async def update_system_prompt_file_handler(request):
    """
    Update system prompt files
    """
    request_json = request.json()

    file_to_content: dict[str, str] = request_json.get("file_to_content", {})
    file_count = len(file_to_content)
    logger.info(f"Updating system prompt: file_count={file_count}, files={list(file_to_content.keys())}")
    result = update_system_prompt_file(file_to_content)
    logger.info(f"System prompt updated: file_count={file_count}")

    return result


@app.get("/character")
async def read_character_handler(request):
    """
    Read character configuration
    """
    logger.debug("Reading character configuration")
    return read_character()


@app.put("/character")
async def write_character_handler(request):
    """
    Write character configuration
    """
    request_json = request.json()

    character_data: dict[str, dict[str, str]] = request_json.get("character_data", {})
    character_count = len(character_data)
    logger.info(
        f"Writing character configuration: character_count={character_count}, keys={list(character_data.keys())}")
    result = write_character(character_data)
    logger.info(f"Character configuration written: character_count={character_count}")

    return result


@app.patch("/character")
async def update_character_handler(request):
    """
    Update character configuration
    """
    request_json = request.json()

    character_data: dict[str, dict[str, str]] = request_json.get("character_data", {})
    character_count = len(character_data)
    logger.info(
        f"Updating character configuration: character_count={character_count}, keys={list(character_data.keys())}")
    result = update_character(character_data)
    logger.info(f"Character configuration updated: character_count={character_count}")

    return result
