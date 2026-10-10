"""TurnRunner — per-session turn lifecycle and drain orchestration.

Turn execution is routed through the user-input queue ():

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
``runtime.session.relation_register`` — half-initialized too). Only stdlib + loguru +
the pure store/model modules (``server.queue.user_input_queue``,
``type.message``) are imported eagerly. The WS-active-task registry is reached
through the lazy :func:`_get_active_tasks` seam; all queue access goes through
:func:`_iqs` at call time (a single monkeypatch point for tests).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from typing import Any
from collections.abc import Callable, Sequence

from loguru import logger

from config.features import WS_STREAM
from server.queue.user_input_queue import UserInputQueueStatus
from server.service.input_queue_service import (
    BatchTurnExecutor,
    TurnInput,
    origin_for_source,
    route_for,
)
from server.service.session_settings_service import promote_pending_settings
from server.service.stream_driver import StreamDriver
from server.service.stream_diag import public_error_text
from server.utils.ws_helpers import send_ws_json
from pub.types.message import MultiModalMessage

# ---------------------------------------------------------------------------
# State + seams (monkeypatch points for tests)
# ---------------------------------------------------------------------------

_DRAIN_TASKS: dict[str, asyncio.Task] = {}
_DRAIN_ERROR_BACKOFF_S: float = WS_STREAM["drain_error_backoff_s"]
_OUTBOUND_ROUTERS: dict[str, Any] = {}


def _promote_pending_project_dir(session_id: str) -> str | None:
    """Seam over the project-dir promotion (lazy import, cycle-safe).

    Returns the promoted directory (truthy) or ``None`` when nothing was parked.
    """
    from server.service.session_project_service import promote_pending_project_dir

    return promote_pending_project_dir(session_id)


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
    from runtime.session.relation_register import relation_register  # noqa: PLC0415

    return relation_register.get_websocket_by_session_id(session_id)


def set_hitl_pending(session_id: str, value: bool) -> None:
    """Seam over session_state's HITL flag writer (lazy, cycle-safe)."""
    from agent.tools.subagent.registry.session_state import (  # noqa: PLC0415
        set_hitl_pending as _set,
    )

    _set(session_id, value)


def is_hitl_pending(session_id: str) -> bool:
    """Seam over session_state's HITL flag reader (lazy, cycle-safe).

    The drain consults this exact flag so it never claims/executes while the
    graph is suspended awaiting a ``hitl_response``.
    """
    from agent.tools.subagent.registry.session_state import (  # noqa: PLC0415
        is_hitl_pending as _read,
    )

    return _read(session_id)


def async_generate(*args: Any, **kwargs: Any) -> Any:
    """Seam over the agent's ``async_generate`` stream (lazy, cycle-safe)."""
    from server.service import async_generate as _generate  # noqa: PLC0415

    return _generate(*args, **kwargs)


def async_generate_multi(*args: Any, **kwargs: Any) -> Any:
    """Seam over the multi-message ``async_generate_multi`` stream (lazy, cycle-safe).

    One drained turn feeds its claimed row(s) as HumanMessages to a SINGLE
    graph call — one message under the per-item drain.
    """
    from server.service import async_generate_multi as _generate  # noqa: PLC0415

    return _generate(*args, **kwargs)


def get_pending_interrupt(*args: Any, **kwargs: Any) -> Any:
    """Seam over the pending-interrupt reader (lazy, cycle-safe)."""
    from server.service import get_pending_interrupt as _read  # noqa: PLC0415

    return _read(*args, **kwargs)


_ACTIVE_TASKS_PROVIDER: Callable[[], dict[str, asyncio.Task]] | None = None


def register_active_tasks_provider(provider: Callable[[], dict[str, asyncio.Task]]) -> None:
    """Register a provider returning the live per-session task registry.

    Upper (trigger) layers own their transport task registries; they push the
    live dict down here at import time so the service layer never has to reach
    upward. Last registration wins.
    """
    global _ACTIVE_TASKS_PROVIDER
    _ACTIVE_TASKS_PROVIDER = provider


def _get_active_tasks() -> dict[str, asyncio.Task]:
    """Live per-session tasks via the registered provider (empty when absent).

    Returns the real registry dict when a trigger layer has registered a
    provider so that adoption and registration stay consistent with the WS
    handler; an empty throwaway dict otherwise (tests patch this seam
    entirely).
    """
    provider = _ACTIVE_TASKS_PROVIDER
    if provider is None:
        return {}
    return provider()


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
    """Send a JSON frame, tolerating a missing socket (frames are skippable).

    Delegates to the shared :func:`server.utils.ws_helpers.send_ws_json`
    (original log wording preserved via ``warn_prefix``).
    """
    await send_ws_json(websocket, payload, warn_prefix="TurnRunner: ws send failed")


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
        logger.exception(f"TurnRunner: outbound router '{route}' failed: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# on_turn_finished + drain loop
# ---------------------------------------------------------------------------


async def on_turn_finished(
    session_id: str, claim_row_ids: str | Sequence[str] | None = None
) -> None:
    """Mark the finished turn's row(s) terminal, then kick the session drain.

    ``claim_row_ids`` identifies the CLAIMED row(s) of the turn that just
    finished (resolved by the executor at its start). Accepts a single id or a
    sequence; the per-item drain claims exactly one row per turn, the sequence
    form covers executors that adopt several rows at once. ``None`` (or an
    empty sequence) means the completion cannot be attributed to a row
    (auto-turn, cancelled turn, resume turn): nothing is marked and the drain
    defers while a foreign CLAIMED row exists.

    Parked control choices (thinking / main model, see
    ``session_settings_service``) are promoted here — before the drain kick, so
    the very next turn already runs on them. A HITL wait is NOT a turn end: the
    suspended graph resumes the same turn, so the promotion happens on its
    completion instead.
    """
    if not is_hitl_pending(session_id):
        try:
            promoted = await promote_pending_settings(session_id)
        except Exception as e:  # pragma: no cover - promotion must never break the turn
            logger.warning(f"TurnRunner: pending control promotion failed for {session_id}: {e}")
        else:
            if promoted:
                logger.info(
                    f"TurnRunner: promoted parked control choice(s) for {session_id}: {promoted}"
                )
        # The project directory parks/promotes exactly like the controls above:
        # same turn boundary, same HITL deferral, one extra key. The promotion
        # itself is silent — the agent hears about the new root from
        # WorkspaceNoticeMiddleware, which compares it against the root the
        # agent was last told about at the START of the next turn.
        try:
            promoted_dir = await asyncio.to_thread(_promote_pending_project_dir, session_id)
            if promoted_dir:
                logger.info("TurnRunner: promoted the parked project directory for {}", session_id)
        except Exception as e:  # pragma: no cover - promotion must never break the turn
            logger.warning(f"TurnRunner: project-dir promotion failed for {session_id}: {e}")

    normalized: list[str] | None
    if claim_row_ids is None:
        normalized = None
    elif isinstance(claim_row_ids, str):
        normalized = [claim_row_ids]
    else:
        normalized = [row_id for row_id in claim_row_ids if row_id is not None]
    if not normalized:
        normalized = None

    if normalized is not None:
        queue = _iqs().get_default_queue()
        for row_id in normalized:
            try:
                await queue.mark_terminal(row_id, UserInputQueueStatus.DELIVERED)
            except Exception as e:
                logger.exception(
                    f"TurnRunner: failed to mark row {row_id} DELIVERED: {e}", exc_info=True
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
    # cancelling check (3.11+): a drain that is being stop-cancelled must
    # not block a fresh drain from starting during the unwind.
    task = asyncio.create_task(_drain_loop(session_id))
    _DRAIN_TASKS[session_id] = task


def _row_group_key(row: Any) -> str:
    """Frozen grouping key: the row's reply_target, or ``"ws"`` when unset."""
    return row.reply_target or "ws"


def _row_route(row: Any) -> str:
    """Registry route kind for a row: ``"channel"`` with a target, else ``"ws"``."""
    return route_for(row.reply_target)


async def _drain_loop(session_id: str) -> None:
    """Execute the session's queued rows ONE AT A TIME in FIFO order.

    Each iteration claims a single QUEUED row and drives it as its own turn, so
    N queued messages produce N turns and N replies — one answer per user
    bubble, in the order they arrived — instead of one merged answer to the
    whole batch. While the session is HITL-pending the loop claims nothing: the
    suspended graph owns the next turn and the resume completion re-kicks the
    drain.
    """
    try:
        while True:
            try:
                if is_hitl_pending(session_id):
                    logger.debug(
                        f"TurnRunner: drain deferred for session {session_id}; "
                        "HITL decision pending"
                    )
                    break
                queue = _iqs().get_default_queue()
                row = await queue.claim_next(session_id)
                if row is None:
                    break
                await _execute_single(session_id, row)
            except Exception as e:
                logger.warning(
                    f"TurnRunner: drain failed for session {session_id}; "
                    f"retrying in {_DRAIN_ERROR_BACKOFF_S}s: {e}"
                )
                await asyncio.sleep(_DRAIN_ERROR_BACKOFF_S)
    finally:
        if _DRAIN_TASKS.get(session_id) is asyncio.current_task():
            _DRAIN_TASKS.pop(session_id, None)


async def _invoke_executor_batch(
    executor: Any, session_id: str, batch: list[TurnInput], reply_target: str | None
) -> None:
    """Drive one route group: ``execute_batch`` when available, else per-row ``execute``.

    Execute-only fakes (legacy tests, custom executors) never had
    ``execute_batch``; they keep working row-by-row.
    """
    execute_batch = getattr(executor, "execute_batch", None)
    if execute_batch is not None:
        await execute_batch(session_id, batch, reply_target)
        return
    for item in batch:
        await executor.execute(session_id, item.message, item.source, reply_target)


async def _execute_single(session_id: str, row: Any) -> None:
    """Execute one claimed row as its own turn; failure finalizes that row.

    A failing row never stops the drain: an unregistered route or an executor
    exception marks THIS row FAILED, sends one error frame, and the loop claims
    the next row.
    """
    queue = _iqs().get_default_queue()
    route = _row_route(row)
    reply_target = row.reply_target
    batch = [
        TurnInput(
            message=_parse_payload_text(row.payload),
            source=row.source,
            message_id=row.client_msg_id,
            claim_row_id=row.id,
        )
    ]
    status = UserInputQueueStatus.DELIVERED
    error_text: str | None = None

    executor = get_registry().resolve(route)
    if executor is None:
        logger.warning(
            f"TurnRunner: no executor registered for route '{route}'; marking row {row.id} FAILED"
        )
        status = UserInputQueueStatus.FAILED
        error_text = f"No executor registered for route '{route}'"
    else:
        try:
            await _invoke_executor_batch(executor, session_id, batch, reply_target)
        except Exception as e:
            logger.warning(
                f"TurnRunner: executor '{route}' failed for session {session_id} (row {row.id}): {e}"
            )
            status = UserInputQueueStatus.FAILED
            error_text = public_error_text(e)

    # The row MUST leave CLAIMED: a claim that is never finalized blocks the
    # dedup key and every later drain deferral for this session until the 24 h
    # recovery sweep. So a failing terminal write falls back to VOIDED (which
    # frees the key) and a failing VOID is logged — never re-raised into the
    # drain loop, whose retry would just claim the NEXT row and leave this one.
    try:
        await queue.mark_terminal(row.id, status)
    except Exception as mark_error:
        logger.exception(
            f"TurnRunner: marking row {row.id} {status} failed for session {session_id}: "
            f"{mark_error}"
        )
        try:
            await queue.mark_terminal(row.id, UserInputQueueStatus.VOIDED)
        except Exception as void_error:
            logger.exception(
                f"TurnRunner: row {row.id} stays CLAIMED (void fallback failed): {void_error}"
            )

    if error_text is not None:
        await _send_turn_error(session_id, route, error_text)


# ---------------------------------------------------------------------------
# WsTurnExecutor
# ---------------------------------------------------------------------------


class WsTurnExecutor(BatchTurnExecutor):
    """TurnExecutor for ws-routed rows: serialize behind live turns, then drive.

    - Resolves its own CLAIMED row(s) at start when the batch does not already
      carry them (idle-branch placeholder: submit inserts it before dispatch;
      the drain claims before executing — both deterministic).
    - If a live task is registered for the session (resume turn), awaits it
      first — never two concurrent streams per session.
    - Drives ``async_generate_multi`` (ONE graph turn for the whole batch) as a
      child task registered in the WS module's ``_active_tasks`` so ``stop`` /
      ``detect_state`` see it. A cancelled child sends the "stopped" frame and
      its CLAIMED rows are marked VOIDED (freeing their client_msg_id dedup
      keys) — a cancelled row is never left CLAIMED. Cancelling the executor
      itself cancels the child first and awaits its shutdown, so no turn
      outlives its executor.
    - Its finally always calls :func:`on_turn_finished`, which also drains any
      rows queued while this turn was running.
    """

    async def execute_batch(
        self, session_id: str, batch: Sequence[TurnInput], reply_target: str | None
    ) -> None:
        queue = _iqs().get_default_queue()
        batch = await self._resolve_claim_rows(queue, session_id, batch)
        claim_row_ids = [item.claim_row_id for item in batch if item.claim_row_id]
        turn_info: dict[str, Any] = {
            "turn_id": uuid.uuid4().hex,
            "message_ids": [item.message_id for item in batch if item.message_id],
        }
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
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                    # The adopted turn was stopped, not us — keep going.

            websocket = get_websocket_by_session_id(session_id)
            child = asyncio.create_task(self._drive(session_id, batch, websocket, turn_info))
            try:
                await child
            except asyncio.CancelledError:
                current = asyncio.current_task()
                our_cancel = current is not None and current.cancelling() > 0
                if our_cancel:
                    # WE are the one being cancelled (shutdown / outer kill),
                    # not the child: awaiting it only delivers the cancellation
                    # here, so without an explicit cancel the turn would keep
                    # streaming in the background. Cancel it and let its own
                    # shutdown (the stopped frame) finish first — any child
                    # failure while shutting down must not mask our cancellation.
                    child.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await child
                # A cancelled row must never be left CLAIMED: that keeps its
                # client_msg_id dedup key busy and makes every later drain for
                # the session defer. VOID it so the drain moves on — the child
                # has already sent "stopped" in both branches.
                await self._void_claimed_rows(queue, claim_row_ids)
                if our_cancel:
                    raise
                return
            completed = True
        finally:
            if child is not None and active.get(session_id) is child:
                active.pop(session_id, None)
            if completed:
                await on_turn_finished(session_id, claim_row_ids or None)
            else:
                await on_turn_finished(session_id)

    @staticmethod
    async def _void_claimed_rows(queue: Any, claim_row_ids: Sequence[str]) -> None:
        for row_id in claim_row_ids:
            try:
                await queue.mark_terminal(row_id, UserInputQueueStatus.VOIDED)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(f"TurnRunner: failed to VOID cancelled row {row_id}: {e}")

    @staticmethod
    async def _resolve_claim_rows(
        queue: Any, session_id: str, batch: Sequence[TurnInput]
    ) -> list[TurnInput]:
        """Backfill claim rows on a batch that carries none (idle single-send).

        The idle-branch placeholder is the sole CLAIMED row at turn start; its
        id + client_msg_id become this turn's claim_row_id/message_id. A drain
        batch already carries both, so it is returned unchanged.
        """
        if any(item.claim_row_id for item in batch):
            return list(batch)
        try:
            rows = await queue.list_active(session_id)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"TurnRunner: claim-row lookup failed for {session_id}: {e}")
            return list(batch)
        # list_active is FIFO by created_at: a QUEUED row may predate this
        # turn's CLAIMED placeholder (input queued under hitl_pending, crash
        # leftovers). The executor's own row is the CLAIMED one -- exactly one
        # exists at this point.
        claimed = next(
            (row for row in rows if row.status is UserInputQueueStatus.CLAIMED),
            None,
        )
        if claimed is None:
            logger.warning(
                f"TurnRunner: no CLAIMED row found for session {session_id} at turn start"
            )
            return list(batch)
        return [
            TurnInput(
                message=item.message,
                source=item.source,
                message_id=item.message_id or claimed.client_msg_id,
                claim_row_id=claimed.id,
            )
            for item in batch
        ]

    async def _drive(
        self,
        session_id: str,
        batch: Sequence[TurnInput],
        websocket: Any,
        turn_info: dict[str, Any],
    ) -> None:
        """Drive ONE generation for the whole batch (runs as the child task).

        The loop itself is the shared :class:`StreamDriver`
        template; ``_WsTurnStreamDriver`` carries this site's knobs plus the
        turn identity so ``turn_started`` is emitted once before any chunk.
        Cleanup lives in :meth:`execute_batch`'s finally, so ``on_finish`` is a
        no-op here.
        """
        active = _get_active_tasks()
        current = asyncio.current_task()
        if current is not None:
            active[session_id] = current
        # The per-item drain always hands this exactly one TurnInput. The list
        # comprehension stays because `batch[0]` would silently DROP a second
        # item if a future caller passed one — the merge semantics were the bug,
        # not the shape of this line.
        messages = [MultiModalMessage(text=item.message) for item in batch]
        sources = {item.source for item in batch}
        origin = origin_for_source("cron" if "cron" in sources else "user")
        await _WsTurnStreamDriver(session_id, websocket, turn_info).drive(
            async_generate_multi(session_id, messages, origin=origin)
        )


class _WsTurnStreamDriver(StreamDriver):
    """Driver for the queue executor's generation turns (WsTurnExecutor._drive).

    The error frame is surfaced here; the row is still delivered so the drain
    never double-sends an error for the same row.
    """

    async def send_frame(self, payload: dict[str, Any]) -> None:
        await _send_ws(self.websocket, payload)

    async def check_interrupt(self) -> dict[str, Any] | None:
        return await get_pending_interrupt(self.session_id)

    def apply_hitl_pending(self) -> None:
        set_hitl_pending(self.session_id, True)

    def log_interrupt(self, interrupt: dict[str, Any]) -> None:
        logger.info(
            f"TurnRunner: HITL interrupt for session {self.session_id}, "
            f"tool={interrupt.get('tool_name')}"
        )

    def log_cancelled(self) -> None:
        logger.info(f"TurnRunner: generation cancelled: session_id={self.session_id}")

    def log_error(self, exc: Exception, elapsed: float) -> None:
        logger.warning(f"TurnRunner: generation failed: session_id={self.session_id}, error={exc}")

    def public_error(self, exc: Exception) -> str:
        from server.service.stream_diag import public_error_text

        return public_error_text(exc)
