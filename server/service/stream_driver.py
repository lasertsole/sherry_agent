"""Shared WS stream driver for turn generators (audit 2.1.3).

Three sites drive an ``async_generate``-style chunk stream and forward the
same WS frame contract — the agent WS handler's ``_run_stream``
(``server/trigger/ws/messages.py``), the queue executor's
``WsTurnExecutor._drive`` (``server/service/turn_runner.py``) and the idle
auto-turn's ``_drive_turn`` (``server/service/auto_turn.py``):

    consume stream → forward ``chunk`` frames (``meta`` captured aside) →
    post-stream HITL-interrupt / done detection → ``stopped`` on cancel /
    ``error`` on failure → per-site finally cleanup.

This module extracts that loop as a Template Method. Each site defines a
small subclass overriding only its site-specific knobs:

- ``send_frame``        — the site's best-effort send (its own ``_send_ws``,
                          preserving per-site ``ensure_ascii`` / log wording);
- ``check_interrupt``   — the site's pending-interrupt lookup, resolved from
                          the site's module globals at call time so test
                          monkeypatching of the site module keeps working;
- ``apply_hitl_pending``— the site's HITL flag writer (the auto turn
                          historically never set the flag → keeps the no-op);
- ``log_*`` hooks       — the site's log wording/levels (the auto turn logs
                          nothing on interrupt/cancel);
- ``on_finish``         — the site's finally cleanup (task-slot release +
                          ``on_turn_finished(claim_row_id)`` for the WS
                          handler; no-op for the queue executor, whose
                          ``execute()`` finally owns cleanup; ``on_turn_finished``
                          with no row for the auto turn);
- ``done_*`` attrs      — the done-frame fallbacks when no ``meta`` chunk
                          arrived. Wire-visible per site: the ws paths send
                          ``""``/``0`` while the auto turn historically sends
                          ``null``.

Only stdlib + loguru are imported here so ``turn_runner`` can import this
module eagerly without the circular-import poison described in its docstring.
"""

import asyncio
import time
from typing import Any
from collections.abc import AsyncGenerator


class StreamDriver:
    done_model_name: Any = ""
    done_input_tokens: Any = 0
    done_output_tokens: Any = 0
    done_finish_reason: Any = ""

    def __init__(self, session_id: str, websocket: Any) -> None:
        self.session_id = session_id
        self.websocket = websocket

    # ---- site hooks -----------------------------------------------------

    async def send_frame(self, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    async def check_interrupt(self) -> dict[str, Any] | None:
        raise NotImplementedError

    def apply_hitl_pending(self) -> None:
        """Flag the session as HITL-pending after a hitl_request frame."""

    def log_interrupt(self, interrupt: dict[str, Any]) -> None:
        """Log the detected HITL interrupt (site wording)."""

    def log_cancelled(self) -> None:
        """Log the cancel (site wording; the auto turn logs nothing)."""

    def log_error(self, exc: Exception, elapsed: float) -> None:
        """Log the failure (site wording/level)."""

    async def on_finish(self) -> None:
        """Site cleanup, awaited on every exit path."""

    # ---- template -------------------------------------------------------

    async def drive(self, source: AsyncGenerator[dict[str, str]]) -> None:
        """Consume *source* to completion, forwarding the turn's WS frames."""
        start_time = time.time()
        meta: dict[str, Any] = {}
        try:
            async for chunk in source:
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("type") == "meta":
                    # Model metadata travels to the client on the done frame,
                    # not as a regular chunk.
                    meta = {k: v for k, v in chunk.items() if k != "type"}
                    continue
                await self.send_frame({"event": "chunk", "session_id": self.session_id, **chunk})

            # After the stream ends, check if the agent paused for HITL approval.
            interrupt = await self.check_interrupt()
            if interrupt:
                self.log_interrupt(interrupt)
                await self.send_frame(
                    {
                        "event": "hitl_request",
                        "session_id": self.session_id,
                        "content": interrupt,
                    }
                )
                self.apply_hitl_pending()
            else:
                await self.send_frame(
                    {
                        "event": "done",
                        "session_id": self.session_id,
                        "content": "",
                        "model_name": meta.get("model_name", self.done_model_name),
                        "input_tokens": meta.get("input_tokens", self.done_input_tokens),
                        "output_tokens": meta.get("output_tokens", self.done_output_tokens),
                        "finish_reason": meta.get("finish_reason", self.done_finish_reason),
                    }
                )
        except asyncio.CancelledError:
            # asyncio.Task.cancel() landed; the generator already yields
            # "Request cancelled" (left inside the stream) and resets answering.
            self.log_cancelled()
            await self.send_frame(
                {
                    "event": "stopped",
                    "session_id": self.session_id,
                    "content": "Request cancelled",
                }
            )
            raise
        except Exception as e:
            elapsed = time.time() - start_time
            self.log_error(e, elapsed)
            await self.send_frame(
                {
                    "event": "error",
                    "session_id": self.session_id,
                    "content": str(e),
                }
            )
        finally:
            await self.on_finish()
