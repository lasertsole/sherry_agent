import uuid
from loguru import logger
from robyn import Response
from server.trigger.core import app
from config import SRC_DIR, API_HOST, API_PORT

_CONTENT_TYPE_TO_EXT: dict[str, str] = {
    "video/mp4": ".mp4",
    "video/mpeg": ".mpeg",
    "video/webm": ".webm",
    "video/ogg": ".ogg",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/x-matroska": ".mkv",
}

_DEFAULT_EXT = ".mp4"


def _get_extension(content_type: str | None) -> str:
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct in _CONTENT_TYPE_TO_EXT:
            return _CONTENT_TYPE_TO_EXT[ct]
    return _DEFAULT_EXT


@app.post("/video/upload")
async def upload_video(request):
    """
    Accept raw video bytes in the request body and persist to src/video/.

    Returns a JSON object with the absolute URL to the stored video file.
    """
    body = request.body

    if isinstance(body, bytes):
        data = body
    elif isinstance(body, str):
        data = body.encode("utf-8")
    else:
        data = b""

    if not body or not data:
        logger.warning("Video upload rejected: empty body")
        return Response(
            status_code=400,
            headers={"Content-Type": "application/json"},
            description='{"success": false, "message": "Empty request body"}',
        )

    content_type = request.headers.get("Content-Type")
    ext = _get_extension(content_type)

    filename = f"{uuid.uuid4().hex}{ext}"

    video_dir = SRC_DIR / "video"
    video_dir.mkdir(parents=True, exist_ok=True)

    file_path = video_dir / filename
    file_path.write_bytes(data)

    url = f"http://{API_HOST}:{API_PORT}/video/{filename}"
    logger.info(f"Video uploaded: filename={filename}, size={len(data)}, url={url}")

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
