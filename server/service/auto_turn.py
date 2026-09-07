"""Idle auto-turn trigger for completion injections (Task 8).

`maybe_trigger_auto_turn` is a fire-and-forget entry point: it snapshots
session idleness via Task 5 `detect_state`, spawns the turn runner as a
background asyncio task, and returns immediately — it NEVER awaits the turn
lifecycle.  The turn itself consumes `server.service.messages.async_generate`
(an async generator) chunk-by-chunk and mirrors the WS frame contract of
`_run_stream`.  Under the new queueing semantics user input arriving during
an auto turn is queued (Task 5/9): the auto turn is never cancelled by user
presence and runs to completion — there is no user-takeover branch.

Hard rules honored here: no import of the WS trigger module (it hangs
standalone), no writes to `_active_tasks` / `answering` / `_pending_args` (all
owned by `async_generate`), no direct SQLite access (Task 6 store is the only
persistence door), loguru-only logging.
"""

import asyncio
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from langchain_core.messages import HumanMessage
from loguru import logger

from server.utils.ws_helpers import send_ws_json
from server.service.stream_driver import StreamDriver
from agent.tools.subagent.announce.steering_queue import enqueue_steering
from agent.tools.subagent.registry.session_keys import normalize_session_key
from agent.tools.subagent.registry.session_state import (
    REASON_AUTO_TURN_INFLIGHT,
    detect_state,
)
from runtime.relation_register import relation_register
from server.service import get_pending_interrupt
from server.service.messages import async_generate
from server.service.turn_runner import on_turn_finished
from type.message import MultiModalMessage


class AutoTurnOutcome(StrEnum):
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


def get_websocket_by_session_id(session_id: str) -> Any:
    """Module-level seam over the RelationManager singleton (tests monkeypatch this)."""
    return relation_register.get_websocket_by_session_id(session_id)


async def _send_ws(websocket: Any, payload: dict[str, Any]) -> None:
    """Best-effort WS delivery; the socket may be gone at any moment.

    Audit 2.1.2: delegates to the shared :func:`server.utils.ws_helpers.send_ws_json`
    (the auto-turn path historically serialized with ``ensure_ascii=False``).
    """
    await send_ws_json(
        websocket,
        payload,
        ensure_ascii=False,
        warn_prefix="auto_turn: websocket send failed",
    )


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
    """Fire-and-forget runner: drive the turn to completion, abandon safely."""
    abandoned = False

    async def _abandon_once() -> None:
        # Never-started / shutdown: persist the injection as PENDING so the
        # next turn consumes it (the turn itself is never abandoned mid-run —
        # user input queues under the new semantics).
        nonlocal abandoned
        if abandoned:
            return
        abandoned = True
        try:
            _item = await enqueue_steering(bare, injection)
        except Exception as exc:  # noqa: BLE001 - abandon must never crash the runner
            logger.error("auto_turn: abandon enqueue failed for {}: {}", bare, exc)

    consumer: asyncio.Task[None] | None = None
    try:
        await asyncio.sleep(0)  # one yield: an already-received user frame can register first
        st = detect_state(bare)
        # WHY exclude auto_turn_inflight: at gate time _INFLIGHT[bare] IS this
        # runner itself — maybe_trigger_auto_turn registered the task BEFORE the
        # task body ran, and its entry gate (above) already returned BUSY /
        # ALREADY_PENDING for genuinely busy or already-running sessions, while
        # _INFLIGHT + _INFLIGHT_LOCK guarantee at most one runner per session.
        # detect_state's frozen precedence (ws_task > answering > hitl_pending >
        # auto_turn_inflight) means a real user race still surfaces as a
        # higher-ranked reason and aborts here. Without the exclusion this gate
        # always self-trips (55d4457 added the signal; the gate predates it).
        if st.busy and st.reason != REASON_AUTO_TURN_INFLIGHT:
            logger.info(
                "auto_turn: session {} went busy before start ({}), abandoning", bare, st.reason
            )
            await _abandon_once()
            return
        consumer = asyncio.ensure_future(_drive_turn(bare, injection))
        try:
            _done, _pending = await asyncio.wait({consumer})
        except asyncio.CancelledError:
            # The runner itself was cancelled (shutdown): tear down and persist PENDING.
            if not consumer.done():
                consumer.cancel()
            # Join the consumer's unwind before abandoning: its finally runs
            # on_turn_finished (queue I/O). Orphaning it here lets event-loop
            # teardown cancel it mid-statement — a first-use _ensure_db cut
            # mid-init poisons the UserInputQueue singleton for every later
            # event loop (order-dependent 10 s stall; F3 VERDICT-queue.md).
            # return_exceptions=True: a cancelled/failed child surfaces as a
            # result, so only OUR re-cancellation raises here.
            try:
                await asyncio.gather(consumer, return_exceptions=True)
            except asyncio.CancelledError:
                pass  # re-cancelled mid-teardown: abandon stays best-effort
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


async def _drive_turn(bare: str, injection: HumanMessage) -> None:
    """Consume the async_generate generator and forward the shared StreamDriver frames.

    Audit 2.1.3: the loop itself is the shared :class:`StreamDriver` template;
    ``_AutoTurnStreamDriver`` carries this site's knobs.
    """
    # Task 4 (subagent-origin-tagging): extract the carrier metadata BEFORE the
    # MultiModalMessage flatten — the flatten to MultiModalMessage drops it, and
    # this is the only place the {internal, provenance, run_id, status} tag can
    # be forwarded into the graph input. Passed VERBATIM to async_generate as
    # origin (getattr-defensive: duck-typed non-BaseMessage injections carry no
    # metadata; None is legal and means "real user" downstream).
    inj_meta = getattr(injection, "metadata", None)
    # Duck-typed on purpose: BaseMessage .text (property, core 1.4.7) preferred,
    # str(content) fallback keeps non-BaseMessage injections from Task 9 usable.
    raw_text = getattr(injection, "text", None)
    text = raw_text if isinstance(raw_text, str) else str(getattr(injection, "content", injection))
    message = MultiModalMessage(text=text)
    websocket = get_websocket_by_session_id(bare)
    await _AutoTurnStreamDriver(bare, websocket).drive(
        async_generate(bare, message, is_stream=True, origin=inj_meta)
    )


class _AutoTurnStreamDriver(StreamDriver):
    """Driver for the idle auto turn (Task 8).

    Hard rules honored: never touches ``_active_tasks`` / ``answering`` /
    ``_pending_args`` (all owned by ``async_generate``) and never sets the
    HITL-pending flag (the auto turn historically never did). The done frame
    falls back to ``null`` (not ``""``/``0``) when no meta chunk arrived —
    the original wire shape of this path.
    """

    done_model_name = None
    done_input_tokens = None
    done_output_tokens = None

    async def send_frame(self, payload: dict[str, Any]) -> None:
        await _send_ws(self.websocket, payload)

    async def check_interrupt(self) -> dict[str, Any] | None:
        return await get_pending_interrupt(self.session_id)

    def log_error(self, exc: Exception, elapsed: float) -> None:
        logger.error("auto_turn: turn failed for {}: {}", self.session_id, exc)

    async def on_finish(self) -> None:
        # Task 7: the auto-turn owns no queue row (claim_row_id=None) — the
        # TurnRunner defers while a foreign CLAIMED row exists, so this only
        # kicks the drain for rows queued while the turn was running.
        await on_turn_finished(self.session_id)
