import asyncio
import uuid
from loguru import logger
from robyn import Response
from server.trigger.core import app
from config import SRC_DIR, API_HOST, API_PORT
from config.features import HTTP_UPLOAD, SERVER_HTTP
from server.service.audio_transcribe_service import (
    TranscribeError,
    TranscribeUnavailableError,
    TranscribeWarmingError,
    probe_status,
    transcribe_wav,
)
from server.trigger.http.helpers import bad_request as _bad_request
from server.trigger.http.helpers import ok as _ok
from server.trigger.http.helpers import to_text_response

MAX_AUDIO_UPLOAD_BYTES = HTTP_UPLOAD["max_audio_bytes"]

_CONTENT_TYPE_TO_EXT: dict[str, str] = SERVER_HTTP["audio_content_type_to_ext"]

_DEFAULT_EXT = SERVER_HTTP["audio_default_ext"]


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

    if len(data) > MAX_AUDIO_UPLOAD_BYTES:
        logger.warning(f"Audio upload rejected: too large ({len(data)} bytes)")
        return Response(
            status_code=413,
            headers={"Content-Type": "application/json"},
            description='{"success": false, "message": "File too large"}',
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
        description=(f'{{"success": true, "url": "{url}", "filename": "{filename}"}}'),
    )


@app.get("/audio/transcribe/status")
async def transcribe_status_handler(request):
    """
    Availability snapshot for the composer's microphone button.

    ``model_dir_present`` is the configuration truth (SenseVoice weights on
    disk); ``daemon_ready`` is informational — a cold daemon spawns on the
    first transcribe request, so a false value here is not a misconfiguration.
    """
    return _ok({"success": True, **probe_status()})


@app.post("/audio/transcribe")
async def transcribe_audio_handler(request):
    """
    Accept raw audio bytes (the browser sends 16 kHz mono PCM WAV), hand them
    to the STT daemon through the service, and answer the transcript.

    Failure contract (machine readable, the client branches on it):
      400 {"success": false, "message": ...}  — empty body
      413 {"success": false, "message": ...}  — body over the upload cap
      503 {"success": false, "warming": true}     — daemon cold-starting, retry
      503 {"success": false, "unavailable": true} — model weights missing
      500 {"success": false, "message": ...}  — transcription failed
    """
    body = request.body
    if isinstance(body, bytes):
        data = body
    elif isinstance(body, str):
        data = body.encode("utf-8")
    else:
        data = b""

    if not data:
        return _bad_request("empty request body")
    if len(data) > MAX_AUDIO_UPLOAD_BYTES:
        logger.warning(f"Transcribe rejected: too large ({len(data)} bytes)")
        return to_text_response(413, {"success": False, "message": "File too large"})

    ext = _get_extension(request.headers.get("Content-Type"))
    try:
        # The daemon call blocks up to READY_WAIT + HTTP_TIMEOUT; keep it off
        # the event loop so one transcription cannot stall the worker.
        result = await asyncio.to_thread(transcribe_wav, data, ext)
    except TranscribeWarmingError:
        return to_text_response(
            503, {"success": False, "warming": True, "message": "STT model is cold-starting"}
        )
    except TranscribeUnavailableError:
        return to_text_response(
            503,
            {"success": False, "unavailable": True, "message": "STT model is not configured"},
        )
    except TranscribeError as error:
        logger.warning("Transcribe failed: {}", error)
        return to_text_response(500, {"success": False, "message": str(error)})
    except Exception as error:  # noqa: BLE001 - a proxy must not 500 raw
        logger.exception("Transcribe crashed: {}", error)
        return to_text_response(500, {"success": False, "message": type(error).__name__})

    return _ok({"success": True, "text": result["text"], "elapsed_ms": result["elapsed_ms"]})
