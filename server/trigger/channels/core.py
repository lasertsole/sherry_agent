import asyncio
import json
from collections.abc import Mapping
from threading import Thread
from typing import Any

from loguru import logger

from runtime import relation_register
from server.queue.user_input_queue import UserInputQueueStatus
from server.service import async_generate
from server.service import input_queue_service as iqs
from type.message import MultiModalMessage
from channels import BaseChannel, channel_manager
from type.bus import InboundMessage, OutboundMessage
from skills.builtin.core.heartbeat import heartbeat_service
from server.service import process_heartbeat_task, process_heartbeat_notify
from pub_func import string_to_unique_int

"""Channel inbound message handler"""

# Route key for the channel executor in the Task 5 registry.
_ROUTE_CHANNEL: str = iqs.ROUTE_CHANNEL

# Cron jobs publish InboundMessage with this sender_id
# (skills/builtin/core/cron/scripts/base.py::_on_cron_job). G2: source
# classification comes from sender_id ONLY -- never from message metadata.
_CRON_SENDER_ID = "cron tool"

_turn_runner_module = None


def _get_turn_runner():
    """Lazy seam to the Task 7 turn runner module.

    Imported on first use (not at module import) so channels/core.py can be
    loaded in isolation and so a mid-write turn_runner module can never
    break channel consumer startup.
    """
    global _turn_runner_module
    if _turn_runner_module is None:
        from server.service import turn_runner as _module

        _turn_runner_module = _module
    return _turn_runner_module


def _build_reply_target(message: InboundMessage) -> str:
    """Reply-target JSON captured at ENQUEUE time (Metis assumption #1).

    The executor/router rebuild the delivery target from THIS blob, never
    from the live relation map, so a reply lands where the message arrived
    even if the session switched chats while the row waited in the queue.
    ``message_id`` (QQ passive-reply msg_id) is preserved for the outbound
    frame's metadata.
    """
    return json.dumps(
        {
            "channel": message.channel,
            "chat_id": message.chat_id,
            "message_id": message.metadata.get("message_id"),
        },
        ensure_ascii=False,
    )


def _parse_reply_target(session_id: str, reply_target: str | None) -> dict[str, Any] | None:
    """Parse the enqueue-time reply-target JSON.

    Returns None for a missing target (nothing to reply to). A present but
    malformed/unusable blob raises RuntimeError so the executor finalizes
    the row as FAILED (drain sends an error frame) instead of silently
    dropping the user's queued message.
    """
    if not reply_target:
        return None
    try:
        target = json.loads(reply_target)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"malformed reply_target for session {session_id}: {reply_target!r}"
        ) from exc
    if not isinstance(target, dict) or not target.get("channel") or target.get("chat_id") is None:
        raise RuntimeError(f"unusable reply_target for session {session_id}: {reply_target!r}")
    return target


async def _send_reply(target: dict[str, Any], content: str, message_id: str | None = None) -> None:
    """Deliver one outbound frame to the enqueue-time channel/chat.

    Best-effort: a vanished channel logs a warning and returns; delivery
    errors are logged and must never crash the consumer loop.
    """
    channel_name: str = target["channel"]
    channel = channel_manager.get_channel(channel_name)
    if channel is None:
        logger.warning(
            "reply target channel {} not registered; dropping outbound frame", channel_name
        )
        return
    metadata: dict[str, Any] = {"message_id": message_id} if message_id else {}
    try:
        await channel.send(
            OutboundMessage(
                channel=channel_name, chat_id=target["chat_id"], content=content, metadata=metadata
            )
        )
    except Exception:
        logger.exception("channel send failed for {} / {}", channel_name, target.get("chat_id"))


class _ChannelTurnExecutor:
    """Task 5 TurnExecutor for channel routes (route="channel").

    Drives one full agent turn for a session, replying to the ENQUEUE-TIME
    reply_target, then hands turn completion to the Task 7 turn runner
    (``on_turn_finished`` marks the CLAIMED placeholder DELIVERED and drains
    the session's QUEUED rows).
    """

    async def execute(self, session_id: str, message: str, source: str, reply_target: str | None) -> None:
        claim_row_id = await self._resolve_claim_row_id(session_id)
        completed = False
        try:
            await self._drive_turn(session_id, message, reply_target)
            completed = True
        finally:
            if not completed and claim_row_id is not None:
                # A crashed/malformed turn must not leave the placeholder
                # CLAIMED (an orphaned placeholder blocks the session until
                # the 24h recover() sweep): finalize it FAILED so the drain
                # can report the error frame instead.
                try:
                    await iqs.get_default_queue().mark_terminal(claim_row_id, "FAILED")
                except Exception:
                    logger.exception(
                        "failed to mark row {} FAILED for session {}", claim_row_id, session_id
                    )
            try:
                turn_runner = _get_turn_runner()
                await turn_runner.on_turn_finished(session_id, claim_row_id if completed else None)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("on_turn_finished failed for session {}", session_id)

    async def _resolve_claim_row_id(self, session_id: str) -> str | None:
        """The CLAIMED placeholder row insert_claimed wrote for this turn."""
        rows = await iqs.get_default_queue().list_active(session_id)
        # list_active is FIFO by created_at: a QUEUED row may predate this
        # turn's CLAIMED placeholder (input queued under hitl_pending, crash
        # leftovers). Mirror WsTurnExecutor (turn_runner.py, fix 32a5d2f):
        # only the CLAIMED row is this turn's placeholder.
        claimed = next(
            (row for row in rows if row.status is UserInputQueueStatus.CLAIMED),
            None,
        )
        if claimed is None:
            logger.warning("no CLAIMED placeholder row found for session {}", session_id)
            return None
        return claimed.id

    async def _drive_turn(self, session_id: str, message: str, reply_target: str | None) -> None:
        target = _parse_reply_target(session_id, reply_target)
        if target is None:
            # Environmental: the channel route always carries a reply_target
            # (route_for only resolves "channel" for non-null targets).
            logger.warning("channel turn for session {} has no reply_target; dropping", session_id)
            return

        user_input: MultiModalMessage = MultiModalMessage(text=message)
        ai_reply: str = ""
        stream = async_generate(session_id=session_id, multi_modal_message=user_input, is_stream=False)
        async for item in stream:
            ai_reply += item["content"]

        await _send_reply(target, ai_reply, message_id=target.get("message_id"))


class _ChannelOutboundRouter:
    """Task 5 OutboundRouter for channel sessions.

    ``frame`` may carry a ``reply_target`` JSON string (enqueue-time target
    wins); otherwise the live session->chat relation map is the fallback.
    ``send_error`` is the Task 7 drain error seam (best-effort via the live
    relation map).
    """

    async def send(self, session_id: str, frame: Mapping[str, object]) -> None:
        content = str(frame.get("content", ""))
        target: dict[str, Any] | None = None
        message_id: str | None = None
        reply_target = frame.get("reply_target")
        if reply_target:
            try:
                target = _parse_reply_target(session_id, str(reply_target))
                message_id = target.get("message_id") if target else None
            except RuntimeError:
                logger.warning(
                    "router frame for session {} carries unusable reply_target {}",
                    session_id,
                    reply_target,
                )
        if target is None:
            target = self._resolve_live_target(session_id)
        if target is None:
            logger.warning("no channel target for session {}; dropping outbound frame", session_id)
            return
        await _send_reply(target, content, message_id=message_id)

    async def send_error(self, session_id: str, content: str) -> None:
        target = self._resolve_live_target(session_id)
        if target is None:
            logger.warning("no channel target for session {}; dropping error frame", session_id)
            return
        await _send_reply(target, content)

    @staticmethod
    def _resolve_live_target(session_id: str) -> dict[str, Any] | None:
        relation = relation_register.get_channel_chat_id_by_session_id(session_id)
        if relation is None:
            return None
        channel_id, chat_id = relation
        return {"channel": channel_id, "chat_id": chat_id}


def register_channel_turn_infra() -> None:
    """Bind the channel TurnExecutor and OutboundRouter into the shared seams.

    Called once at module import (below). The turn-runner registration is
    guarded: if the turn runner cannot be resolved the channel consumer must
    still boot (the executor registration alone keeps the queue contract
    whole; rows would pile up but nothing crashes).
    """
    iqs.get_default_registry().register(_ROUTE_CHANNEL, _ChannelTurnExecutor())
    try:
        _get_turn_runner().register_outbound_router(_ROUTE_CHANNEL, _ChannelOutboundRouter())
    except Exception:
        logger.exception("could not register channel outbound router; continuing without it")


async def _process_inbound(message: InboundMessage, channel: BaseChannel) -> None:
    # Session ID is derived from the channel name only
    session_id: str = str(string_to_unique_int(channel.name))

    # Register channel session (idempotent)
    relation_register.register_channel_chat(
        session_id=session_id, channel_id=channel.name, chat_id=message.chat_id
    )

    # G2 source marking: cron publishes InboundMessage(sender_id="cron tool");
    # everything else is a user message. Metadata is NEVER trusted for this.
    source = "cron" if message.sender_id == _CRON_SENDER_ID else "user"
    # QQ reconnect redeliveries reuse the passive msg_id -> global dedup key.
    client_msg_id = message.metadata.get("message_id")
    # Capture where the reply must go NOW (enqueue time), not at drain time.
    reply_target = _build_reply_target(message)

    # NOTE: media/image URLs are intentionally not converted here anymore:
    # the queue payload convention (Task 5) is text-only
    # ({"text": ..., "image_base64_list": []}), isomorphic with the WS path.

    result = await iqs.submit_user_input(
        session_id=session_id,
        message=message.content,
        source=source,
        reply_target=reply_target,
        client_msg_id=client_msg_id,
    )

    if result.status is iqs.SubmitStatus.STARTED:
        # Turn dispatched by submit; the executor owns reply + drain.
        return
    if result.status is iqs.SubmitStatus.QUEUED:
        logger.debug(
            "channel input for session {} queued at position {} (source={})",
            session_id,
            result.position,
            source,
        )
        return
    if result.status is iqs.SubmitStatus.QUEUE_FULL:
        logger.warning(
            "input queue full for session {} (source={}); dropping channel message",
            session_id,
            source,
        )
        return
    # DEDUPED: duplicate delivery (client_msg_id already active) -- silent.


# Bind the channel executor + outbound router into the Task 5/7 seams.
register_channel_turn_infra()

# Set channel inbound consumer
channel_manager.set_inbound_consumer(_process_inbound)


async def _process_outbound(message: OutboundMessage, channel: BaseChannel) -> None:
    session_id: str = str(string_to_unique_int(channel.name))

    # Register channel session (idempotent)
    relation_register.register_channel_chat(
        session_id=session_id, channel_id=channel.name, chat_id=message.chat_id
    )


# Set channel outbound consumer
channel_manager.set_outbound_consumer(_process_outbound)
"""End channel inbound/outbound handlers"""

"""Heartbeat event handler"""


async def _process_heartbeat_task(task: str) -> str:
    return await process_heartbeat_task(task=task)


heartbeat_service.on_execute = _process_heartbeat_task


async def _process_heartbeat_notify(agent_res: str) -> None:
    return await process_heartbeat_notify(agent_res)


heartbeat_service.on_notify = _process_heartbeat_notify
"""End heartbeat event handler"""


def _run() -> None:
    # Get the event loop from the channel manager so heartbeat and cron services share the same loop
    event_loop = channel_manager.get_event_loop()

    # Start heartbeat service
    asyncio.run_coroutine_threadsafe(heartbeat_service.start(), event_loop)
    # Start channel manager (internally calls run_forever)
    channel_manager.start_service()

    try:
        event_loop.run_forever()
    except Exception:
        pass


_channel_thread: Thread = Thread(target=_run, daemon=True)
_channel_thread.start()
