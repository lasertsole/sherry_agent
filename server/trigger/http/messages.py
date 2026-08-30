from loguru import logger
from server.trigger.core import app
from server.service import (
    clear_session,
    get_history_by_turn_page as _get_history_by_turn_page,
    get_session_list as _get_session_list,
    get_pending_interrupt as _get_pending_interrupt,
)


@app.get("/sessions")
async def get_sessions_handler(request):
    """
    Enumerate all distinct sessions, newest activity first.

    Returns a list of {"session_id", "last_time", "title"} dicts.
    """
    session_list = _get_session_list()
    logger.debug(f"Enumerated sessions: count={len(session_list)}")
    return session_list


@app.delete("/sessions")
async def clear_session_handler(request):
    request_json = request.json()

    session_id: str | None = request_json.get("session_id", None)
    logger.info(f"Clearing session: session_id={session_id}")
    await clear_session(session_id=session_id)
    logger.info(f"Session cleared: session_id={session_id}")


@app.get("/get_history_by_turn_page")
async def get_history_by_turn_page(request):
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

    return _get_history_by_turn_page(session_id, min_turn_num, turn_page_size, turn_page_num)


@app.get("/get_pending_interrupt")
async def get_pending_interrupt_handler(request):
    """
    Return the pending HITL interrupt payload for a session, or ``null`` if none.

    This lets the client restore an in-flight approval card after a session
    switch, page refresh, or browser restart — the interrupt is re-derived
    from the persisted LangGraph checkpoint rather than only pushed over an
    active WebSocket stream.

    Query parameters:
        session_id (str, required): Session ID to inspect.

    Returns one of:
        {"tool_name": str, "tool_args": dict, "description": str, "allowed_decisions": list[str]}
        null
    """
    query_params = request.query_params

    session_id: str | None = query_params.get("session_id", None)
    logger.debug(f"Reading pending HITL interrupt: session_id={session_id}")

    if not session_id:
        raise ValueError("session_id is required")

    return await _get_pending_interrupt(session_id)
