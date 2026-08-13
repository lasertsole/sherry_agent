from server.trigger.core import app
from loguru import logger
from robyn import Response
from server.service import (read_system_prompt_file, write_system_prompt_file, update_system_prompt_file, read_character,
                            write_character, update_character)
from config import STATIC_DIR
import uuid

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
    write_character(character_data)
    logger.info(f"Character configuration written: character_count={character_count}")


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
    update_character(character_data)
    logger.info(f"Character configuration updated: character_count={character_count}")


_CONTENT_TYPE_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}

_DEFAULT_EXT = ".png"


@app.post("/character/avatar")
async def upload_avatar_handler(request):
    """
    Upload avatar image

    Accept raw image bytes in the request body and persist to static/avatar/.
    Returns a JSON object with the relative path to the stored avatar (e.g. avatar/xxx.png).
    """
    body = request.body

    if isinstance(body, bytes):
        data = body
    elif isinstance(body, str):
        data = body.encode("utf-8")
    else:
        data = b""

    if not body or not data:
        logger.warning("Avatar upload rejected: empty body")
        return Response(
            status_code=400,
            headers={"Content-Type": "application/json"},
            description='{"success": false, "message": "Empty request body"}',
        )

    content_type = request.headers.get("Content-Type")
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        ext = _CONTENT_TYPE_TO_EXT.get(ct, _DEFAULT_EXT)
    else:
        ext = _DEFAULT_EXT

    filename = f"{uuid.uuid4().hex}{ext}"

    avatar_dir = STATIC_DIR / "avatar"
    avatar_dir.mkdir(parents=True, exist_ok=True)

    file_path = avatar_dir / filename
    file_path.write_bytes(data)

    logger.info(f"Avatar uploaded: filename={filename}, size={len(data)}")
    return {"path": f"avatar/{filename}"}
