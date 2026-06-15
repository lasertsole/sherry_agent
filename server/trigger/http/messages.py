import time
from .core import app
from loguru import logger
from type import MultiModalMessage
from runtime import state_register
from robyn import SSEMessage, SSEResponse
from server.service import async_generate, clear_session


@logger.catch
@app.post("/sessions/agent/sse")
async def stream_async_events_handler(request):
    start_time = time.time()

    content_type = request.headers.get("content-type") or ""

    if "application/json" in content_type:
        request_json = request.json()
        session_id = request_json.get("session_id", None)
        multi_modal_message = request_json.get("multi_modal_message", None)
    elif "multipart/form-data" in content_type:
        try:
            form_data = request.form_data
            logger.debug(f"form_data={form_data}")
            files = request.files
            logger.debug(f"files={files}")

            session_id = form_data.get("session_id", None)
            query: str = str(form_data.get("query", ""))

            audio_bytes: bytes | None = files.get("audio_bytes") or None
            video_bytes: bytes | None = files.get("video_bytes") or None

            multi_modal_message = {
                "text": query,
                "audio_bytes_list": [audio_bytes],
                "video_bytes_list": [video_bytes],
            }
        except Exception as e:
            logger.exception(f"multipart branch error: {e}")
            raise
    else:
        session_id = None
        multi_modal_message = None

    if not session_id:
        logger.warning("SSE request missing session_id")
        return SSEMessage("Please provide a session ID")

    if not multi_modal_message:
        logger.warning(f"SSE request missing multi_modal_message: session_id={session_id}")
        return SSEMessage("Please provide user input")

    multi_modal_message = MultiModalMessage(**multi_modal_message)

    # Log request summary
    text_preview = multi_modal_message.text[:50] if multi_modal_message.text else ""
    image_count = len(multi_modal_message.image_base64_list) if multi_modal_message.image_base64_list else 0
    logger.info(
        f"SSE request started: session_id={session_id}, "
        f"text_preview='{text_preview}', image_count={image_count}"
    )

    try:
        response = SSEResponse(async_generate(session_id, multi_modal_message))
        elapsed = time.time() - start_time
        logger.info(
            f"SSE request completed: session_id={session_id}, duration={elapsed:.2f}s"
        )
        return response
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(
            f"SSE request failed: session_id={session_id}, duration={elapsed:.2f}s, error={str(e)}"
        )
        raise

@app.post("/sessions/agent/sse/stop")
def stream_async_stop_handler(request):
    request_json = request.json()
    session_id = request_json.get("session_id", None)
    state_register.set_state(session_id, "answering", False)

@app.delete("/sessions")
async def clear_session_handler(request):
    request_json = request.json()

    session_id: str | None = request_json.get("session_id", None)
    logger.info(f"Clearing session: session_id={session_id}")
    await clear_session(session_id=session_id)
    logger.info(f"Session cleared: session_id={session_id}")