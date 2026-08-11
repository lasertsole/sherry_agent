from pathlib import Path
from loguru import logger
from robyn import Response
from server.trigger.core import app
from config import SRC_DIR

# Sorted by specificity; only the extension is used to pick a Content-Type.
_CONTENT_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".webm": "video/webm",
    ".mp4": "video/mp4",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
}

# Media files are written by multimodal_processor to
# <SRC_DIR>/<session_id>/media/<filename>
MEDIA_DIR_NAME = "media"


def _validate_session_id(session_id: str) -> bool:
    """Reject path traversal attempts and empty/odd values in session id."""
    if not session_id:
        return False
    if session_id in (".", ".."):
        return False
    if "/" in session_id or "\\" in session_id:
        return False
    try:
        candidate = (SRC_DIR / session_id).resolve()
    except Exception:
        return False
    # The resolved path must remain inside SRC_DIR to prevent traversal.
    return candidate.is_relative_to(SRC_DIR.resolve())


@app.get("/media")
async def get_media(request):
    """
    Read a persisted media file (image/audio/video) by session and filename.

    Query parameters:
        session_id (str, required): Session ID.
        filename   (str, required): File name inside the session's media/ folder.
    """
    query_params = request.query_params

    session_id: str | None = query_params.get("session_id", None)
    filename: str | None = query_params.get("filename", None)
    logger.debug(f"Reading media: session_id={session_id}, filename={filename}")

    if not session_id or not filename:
        raise ValueError("session_id and filename are required")

    if not _validate_session_id(session_id):
        raise ValueError("invalid session_id")

    # Restrict the filename to a plain file name (no separators/path traversal).
    candidate = Path(filename)
    if candidate.name != filename or candidate.name in (".", ".."):
        raise ValueError("invalid filename")

    media_dir = (SRC_DIR / session_id / MEDIA_DIR_NAME).resolve()
    file_path = (media_dir / candidate.name).resolve()
    if not file_path.is_relative_to(media_dir):
        raise ValueError("invalid filename")

    if not file_path.is_file():
        raise ValueError("media not found")

    data = file_path.read_bytes()
    content_type = _CONTENT_TYPES.get(file_path.suffix.lower(), "application/octet-stream")
    logger.debug(f"Media served: session_id={session_id}, filename={filename}, bytes={len(data)}")
    return Response(status_code=200, headers={"Content-Type": content_type}, description=data)
