import uuid
from loguru import logger
from robyn import Response
from server.trigger.core import app
from config import SRC_DIR, API_HOST, API_PORT

_CONTENT_TYPE_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}

_DEFAULT_EXT = ".png"


def _get_extension(content_type: str | None) -> str:
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct in _CONTENT_TYPE_TO_EXT:
            return _CONTENT_TYPE_TO_EXT[ct]
    return _DEFAULT_EXT


@app.post("/images/upload")
async def upload_image(request):
    """
    Accept raw image bytes in the request body and persist to src/images/.

    Returns a JSON object with the absolute URL to the stored image.
    """
    body = request.body

    if isinstance(body, bytes):
        data = body
    elif isinstance(body, str):
        data = body.encode("utf-8")
    else:
        data = b""

    if not body or not data:
        logger.warning("Image upload rejected: empty body")
        return Response(
            status_code=400,
            headers={"Content-Type": "application/json"},
            description='{"success": false, "message": "Empty request body"}',
        )

    content_type = request.headers.get("Content-Type")
    ext = _get_extension(content_type)

    filename = f"{uuid.uuid4().hex}{ext}"

    images_dir = SRC_DIR / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    file_path = images_dir / filename
    file_path.write_bytes(data)

    url = f"http://{API_HOST}:{API_PORT}/images/{filename}"
    logger.info(f"Image uploaded: filename={filename}, size={len(data)}, url={url}")

    return Response(
        status_code=200,
        headers={"Content-Type": "application/json"},
        description=(f'{{"success": true, "url": "{url}", "filename": "{filename}"}}'),
    )
