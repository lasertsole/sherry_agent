import time
from loguru import logger
from server.trigger.core import app
from runtime import state_register_mem
from robyn import SSEMessage, SSEResponse
from type.message import MultiModalMessage
from server.service import async_generate, clear_session, get_history_by_page as _get_history_by_page


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
    state_register_mem.set_state(session_id, "answering", False)

@app.delete("/sessions")
async def clear_session_handler(request):
    request_json = request.json()

    session_id: str | None = request_json.get("session_id", None)
    logger.info(f"Clearing session: session_id={session_id}")
    await clear_session(session_id=session_id)
    logger.info(f"Session cleared: session_id={session_id}")

@app.get("/get_history_by_page")
async def get_history_by_page(request):
    """
    Read history messages with pagination.

    Query parameters:
        session_id (str, required):     Session ID.
        min_turn_num (int, required):   Minimum turn number (>= 1). Turns below this are excluded.
        turn_page_size (int, required): Turns per page (>= 1).
        turn_page_num (int, required):  Page number (>= 1). 1 = most recent page.
    """
    query_params = request.query_params

    session_id: str | None = query_params.get("session_id", None)
    min_turn_num: int | None = query_params.get("min_turn_num", None)
    turn_page_size: int | None = query_params.get("turn_page_size", None)
    turn_page_num: int | None = query_params.get("turn_page_num", None)
    logger.debug(f"Reading history messages: session_id={session_id}")

    if not session_id:
        raise ValueError("session_id is required")

    if not min_turn_num:
        raise ValueError("last_turn_count is required")

    if not turn_page_size:
        raise ValueError("turn_page_size is required")

    if not turn_page_num:
        raise ValueError("turn_page_num is required")

    return _get_history_by_page(session_id, min_turn_num, turn_page_size, turn_page_num)