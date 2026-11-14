from server.trigger.core import app
from loguru import logger
from server.trigger.http.helpers import bad_request, read_body
from server.service import (
    read_system_prompt_file,
    write_system_prompt_file,
    update_system_prompt_file,
    write_preset_system_prompt_file,
    read_system_prompt_template,
)


@app.get("/system_prompt")
async def read_system_prompt_handler(request) -> dict[str, str]:
    """
    Read system prompt files
    """
    logger.debug("Reading system prompt")

    return read_system_prompt_file()


@app.get("/system_prompt/template")
async def read_system_prompt_template_handler(request) -> dict[str, str]:
    """
    Read system prompt template files for a given language (default: user's preferred language).
    """
    query_params = request.query_params or {}
    lang: str | None = query_params.get("lang", None)
    lang = lang.strip().lower() if lang else None
    logger.debug(f"Reading system prompt template: lang={lang}")

    return read_system_prompt_template(lang)


@app.put("/system_prompt")
async def write_system_prompt_file_handler(request):
    """
    Write system prompt files
    """
    request_json = read_body(request)
    if request_json is None:
        return bad_request("JSON body required")

    file_to_content: dict[str, str] = request_json.get("file_to_content", {})
    file_count = len(file_to_content)
    logger.info(
        f"Writing system prompt: file_count={file_count}, files={list(file_to_content.keys())}"
    )
    write_system_prompt_file(file_to_content)
    logger.info(f"System prompt written: file_count={file_count}")


@app.patch("/system_prompt")
async def update_system_prompt_file_handler(request):
    """
    Update system prompt files
    """
    request_json = read_body(request)
    if request_json is None:
        return bad_request("JSON body required")

    file_to_content: dict[str, str] = request_json.get("file_to_content", {})
    file_count = len(file_to_content)
    logger.info(
        f"Updating system prompt: file_count={file_count}, files={list(file_to_content.keys())}"
    )
    update_system_prompt_file(file_to_content)
    logger.info(f"System prompt updated: file_count={file_count}")


@app.put("/system_prompt/preset")
async def write_preset_system_prompt_handler(request):
    """Write a built-in preset's full persona payload (AGENTS.md included).

    The ordinary ``PUT /system_prompt`` refuses AGENTS.md; this creation-only
    route is how a new preset seeds its Operating Instructions.
    """
    request_json = read_body(request)
    if request_json is None:
        return bad_request("JSON body required")
    file_to_content: dict[str, str] = request_json.get("file_to_content", {})
    logger.info(
        "Writing preset system prompt: file_count={}, files={}",
        len(file_to_content),
        list(file_to_content.keys()),
    )
    write_preset_system_prompt_file(file_to_content)
