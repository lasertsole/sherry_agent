"""Idle auto-turn trigger for completion injections (Task 8).

`maybe_trigger_auto_turn` is a fire-and-forget entry point: it snapshots
session idleness via Task 5 `detect_state`, spawns the turn runner as a
background asyncio task, and returns immediately — it NEVER awaits the turn
lifecycle.  The turn itself consumes `server.service.messages.async_generate`
(an async generator) chunk-by-chunk and mirrors the WS frame contract of
`_run_stream`.  A live WS task detected mid-turn (`reason == "ws_task"`) is a
user takeover: the auto turn is cancelled and the injection is re-queued as
PENDING via the Task 6 steering queue (exactly once, never after normal
completion), so no injection is ever lost or duplicated.

Hard rules honored here: no import of the WS trigger module (it hangs
standalone), no writes to `_active_tasks` / `answering` / `_pending_args` (all
owned by `async_generate`), no direct SQLite access (Task 6 store is the only
persistence door), loguru-only logging.
"""

import asyncio
import json
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any

from langchain_core.messages import HumanMessage
from loguru import logger

from agent.tools.subagent.announce.steering_queue import enqueue_steering
from agent.tools.subagent.registry.session_keys import normalize_session_key
from agent.tools.subagent.registry.session_state import detect_state
from runtime.relation_register import relation_register
from server.service import get_pending_interrupt
from server.service.messages import async_generate
from type.message import MultiModalMessage


class AutoTurnOutcome(str, Enum):
    TRIGGERED = "triggered"
    ALREADY_PENDING = "already_pending"
    BUSY = "busy"


@dataclass(frozen=True)
class AutoTurnResult:
    outcome: AutoTurnOutcome
    session_key: str  # normalized BARE id
    reason: str | None = None  # SessionState.reason when BUSY


_INFLIGHT: dict[str, asyncio.Task[None]] = {}  # bare id -> auto-turn runner task
_INFLIGHT_LOCK = threading.Lock()  # NEVER held across await
_USER_TAKEOVER_POLL_SECONDS: float = 0.5


def get_websocket_by_session_id(session_id: str) -> Any:
    """Module-level seam over the RelationManager singleton (tests monkeypatch this)."""
    return relation_register.get_websocket_by_session_id(session_id)


def _send_ws(websocket: Any, payload: dict[str, Any]) -> None:
    """Best-effort WS delivery; the socket may be gone at any moment."""
    if websocket is None:
        return
    try:
        websocket.send_text(json.dumps(payload, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - delivery must never break the turn
        logger.warning("auto_turn: websocket send failed: {}", exc)


async def maybe_trigger_auto_turn(session_key: str, injection: HumanMessage) -> AutoTurnResult:
    """Snapshot idleness and spawn the fire-and-forget auto turn (zero awaits here)."""
    bare = normalize_session_key(session_key)
    if not bare:
        # Not our problem (Task 9 addresses real sessions): zero side effects.
        return AutoTurnResult(AutoTurnOutcome.BUSY, bare, "unknown_session")
    st = detect_state(bare)
    if st.busy:
        return AutoTurnResult(AutoTurnOutcome.BUSY, bare, st.reason)
    with _INFLIGHT_LOCK:
        existing = _INFLIGHT.get(bare)
        if existing is not None and not existing.done():
            return AutoTurnResult(AutoTurnOutcome.ALREADY_PENDING, bare, None)
        task = asyncio.create_task(_run_auto_turn(bare, injection))
        _INFLIGHT[bare] = task
    return AutoTurnResult(AutoTurnOutcome.TRIGGERED, bare, None)


async def _run_auto_turn(bare: str, injection: HumanMessage) -> None:
    """Fire-and-forget runner: drive the turn, watch for user takeover, abandon safely."""
    abandoned = False

    async def _abandon_once() -> None:
        # User-wins: persist the injection as PENDING so the next turn consumes it.
        nonlocal abandoned
        if abandoned:
            return
        abandoned = True
        try:
            _item = await enqueue_steering(bare, injection)
        except Exception as exc:  # noqa: BLE001 - abandon must never crash the runner
            logger.error("auto_turn: abandon enqueue failed for {}: {}", bare, exc)

    consumer: asyncio.Task[None] | None = None
    watcher: asyncio.Task[None] | None = None
    try:
        await asyncio.sleep(0)  # one yield: an already-received user frame can register first
        st = detect_state(bare)
        if st.busy:
            logger.info("auto_turn: session {} went busy before start ({}), abandoning", bare, st.reason)
            await _abandon_once()
            return
        consumer = asyncio.ensure_future(_drive_turn(bare, injection))
        watcher = asyncio.ensure_future(_watch_user_takeover(bare, consumer))
        try:
            _done, _pending = await asyncio.wait({consumer})
        except asyncio.CancelledError:
            # The runner itself was cancelled (shutdown): tear down and persist PENDING.
            if not consumer.done():
                consumer.cancel()
            if not watcher.done():
                watcher.cancel()
            if not consumer.done() or consumer.cancelled():
                try:
                    await asyncio.shield(_abandon_once())
                except asyncio.CancelledError:
                    pass
            raise
        if consumer.cancelled():
            await _abandon_once()
        elif (exc := consumer.exception()) is not None:
            logger.error("auto_turn: turn task crashed for {}: {!r}", bare, exc)
    finally:
        with _INFLIGHT_LOCK:
            if _INFLIGHT.get(bare) is asyncio.current_task():
                _INFLIGHT.pop(bare, None)
        if watcher is not None and not watcher.done():
            watcher.cancel()


async def _drive_turn(bare: str, injection: HumanMessage) -> None:
    """Consume the async_generate generator and forward _run_stream frames."""
    # Duck-typed on purpose: BaseMessage .text (property, core 1.4.7) preferred,
    # str(content) fallback keeps non-BaseMessage injections from Task 9 usable.
    raw_text = getattr(injection, "text", None)
    text = raw_text if isinstance(raw_text, str) else str(getattr(injection, "content", injection))
    message = MultiModalMessage(text=text)
    websocket = get_websocket_by_session_id(bare)
    meta: dict[str, Any] = {}
    try:
        async for chunk in async_generate(bare, message, is_stream=True):
            if isinstance(chunk, dict) and chunk.get("type") == "meta":
                meta.update(chunk)
                continue
            if detect_state(bare).reason == "ws_task":
                # Cheap per-chunk takeover check complementing the watcher.
                task = asyncio.current_task()
                if task is not None:
                    task.cancel()
                break
            _send_ws(websocket, {"event": "chunk", "session_id": bare, **chunk})
        interrupt_data = await get_pending_interrupt(bare)
        if interrupt_data:
            _send_ws(websocket, {"event": "hitl_request", "session_id": bare, "content": interrupt_data})
        else:
            _send_ws(
                websocket,
                {
                    "event": "done",
                    "session_id": bare,
                    "content": "",
                    "model_name": meta.get("model_name"),
                    "input_tokens": meta.get("input_tokens"),
                    "output_tokens": meta.get("output_tokens"),
                },
            )
    except asyncio.CancelledError:
        _send_ws(websocket, {"event": "stopped", "session_id": bare, "content": "Request cancelled"})
        raise
    except Exception as exc:  # noqa: BLE001 - mirror _run_stream error frame
        logger.error("auto_turn: turn failed for {}: {}", bare, exc)
        _send_ws(websocket, {"event": "error", "session_id": bare, "content": str(exc)})


async def _watch_user_takeover(bare: str, turn_task: asyncio.Task[None]) -> None:
    """Poll detect_state; a live ws_task during the auto turn means the user took over."""
    while True:
        await asyncio.sleep(_USER_TAKEOVER_POLL_SECONDS)
        if turn_task.done():
            return
        if detect_state(bare).reason == "ws_task":
            logger.info("auto_turn: user takeover detected on {}, cancelling auto turn", bare)
            turn_task.cancel()
            return
