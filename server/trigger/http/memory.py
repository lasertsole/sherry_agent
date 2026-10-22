import asyncio

from loguru import logger
from server.trigger.core import app
from server.service import read_memory_files, write_memory_files


@app.get("/memory")
async def read_memory_handler(request) -> dict[str, str]:
    """
    Read long-term memory files (workspace/memory/*).

    ``?session_id=`` resolves FACTS.md to that session's bound project
    (``<project>/.sherry/memory/FACTS.md``); MEMORY.md / USER.md stay global.
    """
    session_id = (request.query_params.get("session_id", "") or "").strip() or None
    logger.debug("Reading memory files: session_id={}", session_id)

    # The project-root lookup may hit the SQLite mirror (cold mem tier).
    return await asyncio.to_thread(read_memory_files, session_id)


@app.put("/memory")
async def write_memory_handler(request):
    """
    Write long-term memory files (workspace/memory/*).
    Body: {"file_to_content": {"MEMORY.md": "...", "USER.md": "...", "FACTS.md": "..."},
           "session_id": "<optional>"}
    Only provided files are overwritten; others are left unchanged. A
    ``session_id`` whose session is bound to a project writes FACTS.md under
    that project's ``.sherry/memory/``.
    """
    request_json = request.json()

    file_to_content: dict[str, str] = request_json.get("file_to_content", {})
    session_id = str(request_json.get("session_id") or "").strip() or None
    file_count = len(file_to_content)
    logger.info(
        f"Writing memory files: file_count={file_count}, files={list(file_to_content.keys())}, "
        f"session_id={session_id}"
    )
    await asyncio.to_thread(write_memory_files, file_to_content, session_id)
    logger.info(f"Memory files written: file_count={file_count}")
