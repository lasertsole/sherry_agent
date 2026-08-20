import uuid
from loguru import logger
from robyn import Response
from server.trigger.core import app
from config import SRC_DIR, API_HOST, API_PORT

_CONTENT_TYPE_TO_EXT: dict[str, str] = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
    "audio/flac": ".flac",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/x-flac": ".flac",
}

_DEFAULT_EXT = ".mp3"


def _get_extension(content_type: str | None) -> str:
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct in _CONTENT_TYPE_TO_EXT:
            return _CONTENT_TYPE_TO_EXT[ct]
    return _DEFAULT_EXT


@app.post("/audio/upload")
async def upload_audio(request):
    """
    Accept raw audio bytes in the request body and persist to src/audio/.

    Returns a JSON object with the absolute URL to the stored audio file.
    """
    body = request.body

    if isinstance(body, bytes):
        data = body
    elif isinstance(body, str):
        data = body.encode("utf-8")
    else:
        data = b""

    if not body or not data:
        logger.warning("Audio upload rejected: empty body")
        return Response(
            status_code=400,
            headers={"Content-Type": "application/json"},
            description='{"success": false, "message": "Empty request body"}',
        )

    content_type = request.headers.get("Content-Type")
    ext = _get_extension(content_type)

    filename = f"{uuid.uuid4().hex}{ext}"

    audio_dir = SRC_DIR / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    file_path = audio_dir / filename
    file_path.write_bytes(data)

    url = f"http://{API_HOST}:{API_PORT}/audio/{filename}"
    logger.info(f"Audio uploaded: filename={filename}, size={len(data)}, url={url}")

    return Response(
        status_code=200,
        headers={"Content-Type": "application/json"},
        description=(
            '{"success": true, '
            f'"url": "{url}", '
            f'"filename": "{filename}"'
            '}'
        ),
    )
