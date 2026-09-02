"""TurnRunner — per-session turn lifecycle and drain orchestration (Task 7).

Turn execution is routed through the user-input queue (Task 5):

- A finished turn calls :func:`on_turn_finished` (from the WS handler's
  ``_run_stream`` finally, the ``WsTurnExecutor`` finally, or the auto-turn's
  ``_drive_turn`` finally) to mark its own row terminal and kick a
  **single-flight drain** that executes every queued row of the session FIFO.
- The drain loop claims one QUEUED row at a time, routes it by
  ``reply_target`` (``"ws"`` when unset), executes it, marks it terminal, and
  continues. One failing row is marked FAILED + an error frame is sent, then
  the drain continues — it never crashes mid-queue.
- With ``claim_row_id=None`` the caller does not know which row finished
  (auto-turn completion, cancelled turn, resume turn). In that case the drain
  defers while a foreign CLAIMED row (a live turn's placeholder) exists, so a
  drain never runs concurrently with a live turn. The live turn's own
  completion re-triggers the drain.

Imports: this module has **no eager project-internal imports** — every
``server.service`` / ``agent.*`` / ``runtime.*`` dependency is reached through
a lazy call-time seam defined below. ``turn_runner`` is imported by the WS
handler, by ``auto_turn``, and transitively by half the app; one eager project
import here creates a circular-import poison (a partially-initialized
``turn_runner`` in ``sys.modules`` that other modules' ``from server.service
import turn_runner`` resolve to mid-cycle, leaving *their* imports — e.g.
``runtime.relation_register`` — half-initialized too). Only stdlib + loguru +
the pure store/model modules (``server.queue.user_input_queue``,
``type.message``) are imported eagerly. The WS-active-task registry is reached
through the lazy :func:`_get_active_tasks` seam; all queue access goes through
:func:`_iqs` at call time (a single monkeypatch point for tests).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger

from server.queue.user_input_queue import UserInputQueueStatus
from type.message import MultiModalMessage

# ---------------------------------------------------------------------------
# State + seams (monkeypatch points for tests)
# ---------------------------------------------------------------------------

_DRAIN_TASKS: dict[str, asyncio.Task] = {}
_OUTBOUND_ROUTERS: dict[str, Any] = {}


def _iqs() -> Any:
    """Seam over the user-input queue service module (lazy, cycle-safe).

    ``server.service.*`` pulls in the WS handler, which imports this module —
    the import must happen at call time or this module's body can never finish
    when ``turn_runner`` itself is the import entry point.
    """
    from server.service import input_queue_service  # noqa: PLC0415

    return input_queue_service


def get_registry() -> Any:
    """Seam over the default executor registry."""
    return _iqs().get_default_registry()


def get_websocket_by_session_id(session_id: str) -> Any:
    """Seam over the live socket lookup (lazy, cycle-safe)."""
    from runtime.relation_register import relation_register  # noqa: PLC0415

    return relation_register.get_websocket_by_session_id(session_id)


def set_hitl_pending(session_id: str, value: bool) -> None:
    """Seam over session_state's HITL flag writer (lazy, cycle-safe)."""
    from agent.tools.subagent.registry.session_state import (  # noqa: PLC0415
        set_hitl_pending as _set,
    )

    _set(session_id, value)


def async_generate(*args: Any, **kwargs: Any) -> Any:
    """Seam over the agent's ``async_generate`` stream (lazy, cycle-safe)."""
    from server.service import async_generate as _generate  # noqa: PLC0415

    return _generate(*args, **kwargs)


def get_pending_interrupt(*args: Any, **kwargs: Any) -> Any:
    """Seam over the pending-interrupt reader (lazy, cycle-safe)."""
    from server.service import get_pending_interrupt as _read  # noqa: PLC0415

    return _read(*args, **kwargs)


def _get_active_tasks() -> dict[str, asyncio.Task]:
    """Seam over the WS module's live per-session tasks (lazy, cycle-safe).

    Returns the real registry dict when the WS module is importable so that
    adoption and registration stay consistent with the WS handler; an empty
    throwaway dict otherwise (tests patch this seam entirely).
    """
    try:
        from server.trigger.ws import messages as ws_messages  # noqa: PLC0415

        return ws_messages._active_tasks  # noqa: SLF001
    except Exception:  # pragma: no cover - ws module always present in prod
        return {}


def register_outbound_router(route: str, router: Any) -> None:
    """Register an error-frame router for non-ws routes (e.g. ``channel``)."""
    _OUTBOUND_ROUTERS[route] = router


def register_default_ws_executor() -> None:
    """Register the ws TurnExecutor on the default registry (idempotent)."""
    get_registry().register("ws", WsTurnExecutor())


# ---------------------------------------------------------------------------
# Frame helpers
# ---------------------------------------------------------------------------


async def _send_ws(websocket: Any, payload: dict[str, Any]) -> None:
    """Send a JSON frame, tolerating a missing socket (frames are skippable)."""
    if websocket is None:
        return
    try:
        await websocket.send_text(json.dumps(payload))
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"TurnRunner: ws send failed: {e}")


def _parse_payload_text(payload: str) -> str:
    """Extract the message text from a queue payload (T5 payload contract)."""
    try:
        obj: Any = json.loads(payload)
    except (TypeError, ValueError):
        return str(payload)
    if isinstance(obj, dict):
        return str(obj.get("text", ""))
    return str(obj)


async def _send_turn_error(session_id: str, route: str, content: str) -> None:
    """Route an error frame: ws → socket, other routes → registered router."""
    if route == "ws":
        await _send_ws(
            get_websocket_by_session_id(session_id),
            {"event": "error", "session_id": session_id, "content": content},
        )
        return
    router = _OUTBOUND_ROUTERS.get(route)
    if router is None:
        return
    try:
        await router.send_error(session_id, content)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"TurnRunner: outbound router '{route}' failed: {e}")


# ---------------------------------------------------------------------------
# on_turn_finished + drain loop
# ---------------------------------------------------------------------------


async def on_turn_finished(session_id: str, claim_row_id: str | None = None) -> None:
    """Mark the given turn's row terminal, then kick the session drain.

    ``claim_row_id`` identifies the CLAIMED row of the turn that just finished
    (resolved by the executor at its start). ``None`` means the completion
    cannot be attributed to a row (auto-turn, cancelled turn, resume turn):
    nothing is marked and the drain defers while a foreign CLAIMED row exists.
    """
    if claim_row_id is not None:
        try:
            await _iqs().get_default_queue().mark_terminal(
                claim_row_id, UserInputQueueStatus.DELIVERED
            )
        except Exception as e:
            logger.warning(
                f"TurnRunner: failed to mark row {claim_row_id} DELIVERED: {e}"
            )
    else:
        try:
            rows = await _iqs().get_default_queue().list_active(session_id)
        except Exception as e:
            logger.warning(f"TurnRunner: list_active failed for {session_id}: {e}")
            rows = []
        if any(row.status is UserInputQueueStatus.CLAIMED for row in rows):
            logger.debug(
                f"TurnRunner: deferring drain for session {session_id}; "
                "a live turn is still CLAIMED"
            )
            return

    existing = _DRAIN_TASKS.get(session_id)
    if existing is not None and not existing.done() and existing.cancelling() == 0:
        return
    # cancelling() check (3.11+): a drain that is being stop-cancelled must
    # not block a fresh drain from starting during the unwind.
    task = asyncio.create_task(_drain_loop(session_id))
    _DRAIN_TASKS[session_id] = task


async def _drain_loop(session_id: str) -> None:
    """Execute the session's queued rows FIFO until the queue runs dry."""
    try:
        while True:
            queue = _iqs().get_default_queue()
            row = await queue.claim_next(session_id)
            if row is None:
                break
            await _execute_claimed_row(session_id, row)
    finally:
        if _DRAIN_TASKS.get(session_id) is asyncio.current_task():
            _DRAIN_TASKS.pop(session_id, None)


async def _execute_claimed_row(session_id: str, row: Any) -> None:
    """Execute one claimed row; a single failure never stops the drain."""
    queue = _iqs().get_default_queue()
    route = row.reply_target or "ws"
    executor = get_registry().resolve(route)
    if executor is None:
        logger.warning(
            f"TurnRunner: no executor registered for route '{route}'; "
            f"marking row {row.id} FAILED"
        )
        await queue.mark_terminal(row.id, UserInputQueueStatus.FAILED)
        await _send_turn_error(
            session_id, route, f"No executor registered for route '{route}'"
        )
        return

    message = _parse_payload_text(row.payload)
    try:
        await executor.execute(session_id, message, row.source, row.reply_target)
    except Exception as e:
        logger.warning(
            f"TurnRunner: executor '{route}' failed for session {session_id} "
            f"row {row.id}: {e}"
        )
        await queue.mark_terminal(row.id, UserInputQueueStatus.FAILED)
        await _send_turn_error(session_id, route, str(e))
        return
    await queue.mark_terminal(row.id, UserInputQueueStatus.DELIVERED)


# ---------------------------------------------------------------------------
# WsTurnExecutor
# ---------------------------------------------------------------------------


class WsTurnExecutor:
    """TurnExecutor for ws-routed rows: serialize behind live turns, then drive.

    - Resolves its own CLAIMED row id at start (submit inserts the placeholder
      before dispatch; the drain claims before executing — deterministic).
    - If a live task is registered for the session (resume turn), awaits it
      first — never two concurrent streams per session.
    - Drives ``async_generate`` as a child task registered in the WS module's
      ``_active_tasks`` so ``stop`` / ``detect_state`` see it. A cancelled
      child sends the "stopped" frame; the row's fate then belongs to the T6
      marker (VOIDED) or the drain loop (FAILED), not to this executor.
    - Its finally always calls :func:`on_turn_finished`, which also drains any
      rows queued while this turn was running.
    """

    async def execute(
        self, session_id: str, message: str, source: str, reply_target: str | None
    ) -> None:
        queue = _iqs().get_default_queue()
        claim_row_id = await self._resolve_claim_row_id(queue, session_id)
        active = _get_active_tasks()
        completed = False
        child: asyncio.Task | None = None
        try:
            existing = active.get(session_id)
            if existing is not None and not existing.done() and existing.cancelling() == 0:
                logger.info(
                    f"TurnRunner: adopting live turn for session {session_id} "
                    "before driving the queued row"
                )
                try:
                    await existing
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    # The adopted turn was stopped, not us — keep going.

            websocket = get_websocket_by_session_id(session_id)
            child = asyncio.create_task(self._drive(session_id, message, websocket))
            try:
                await child
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                # Child was cancelled (stop): "stopped" already sent; row fate
                # is decided by the T6 marker / drain loop, not here.
                return
            completed = True
        finally:
            if child is not None and active.get(session_id) is child:
                active.pop(session_id, None)
            if completed:
                await on_turn_finished(session_id, claim_row_id)
            else:
                await on_turn_finished(session_id)

    @staticmethod
    async def _resolve_claim_row_id(queue: Any, session_id: str) -> str | None:
        try:
            rows = await queue.list_active(session_id)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"TurnRunner: claim-row lookup failed for {session_id}: {e}")
            return None
        claim_row_id = rows[0].id if rows else None
        if claim_row_id is None:
            logger.warning(
                f"TurnRunner: no CLAIMED row found for session {session_id} at turn start"
            )
        return claim_row_id

    async def _drive(self, session_id: str, message: str, websocket: Any) -> None:
        """Drive one generation, forwarding frames (runs as the child task)."""
        active = _get_active_tasks()
        active[session_id] = asyncio.current_task()
        meta: dict[str, Any] = {}
        try:
            async for chunk in async_generate(session_id, MultiModalMessage(text=message)):
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("type") == "meta":
                    meta = {k: v for k, v in chunk.items() if k != "type"}
                    continue
                await _send_ws(
                    websocket, {"event": "chunk", "session_id": session_id, **chunk}
                )

            interrupt = await get_pending_interrupt(session_id)
            if interrupt:
                logger.info(
                    f"TurnRunner: HITL interrupt for session {session_id}, "
                    f"tool={interrupt.get('tool_name')}"
                )
                await _send_ws(
                    websocket,
                    {
                        "event": "hitl_request",
                        "session_id": session_id,
                        "content": interrupt,
                    },
                )
                set_hitl_pending(session_id, True)
            else:
                await _send_ws(
                    websocket,
                    {
                        "event": "done",
                        "session_id": session_id,
                        "content": "",
                        "model_name": meta.get("model_name", ""),
                        "input_tokens": meta.get("input_tokens", 0),
                        "output_tokens": meta.get("output_tokens", 0),
                    },
                )
        except asyncio.CancelledError:
            logger.info(f"TurnRunner: generation cancelled: session_id={session_id}")
            await _send_ws(
                websocket,
                {
                    "event": "stopped",
                    "session_id": session_id,
                    "content": "Request cancelled",
                },
            )
            raise
        except Exception as e:
            # The error frame is surfaced here; the row is still delivered so
            # the drain never double-sends an error for the same row.
            logger.warning(
                f"TurnRunner: generation failed: session_id={session_id}, error={e}"
            )
            await _send_ws(
                websocket,
                {"event": "error", "session_id": session_id, "content": str(e)},
            )
