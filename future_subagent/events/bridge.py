"""EventBus → global MessageBus bridge.

Bridges messages published to the future_subagent EventBus into the
project-wide MessageBus so they reach the existing consumption pipeline
(ChannelManager._inbound_consume_loop → SubagentManager._consume_loop →
channel/websocket).

Mirrors OpenClaw's SubagentRegistry lifecycle listener: the EventBus
carries lightweight signal events, the bridge forwards them to the
global MessageBus where existing consumers already handle routing,
LLM personalization, and channel delivery.

Lifecycle:
    - ``start_bridge()`` — called by ``init_registry()`` at service startup
    - ``stop_bridge()``  — called at shutdown
    - The consume loop runs on the current asyncio event loop
"""

from __future__ import annotations

import asyncio
from loguru import logger

from .core import EventBus, InboundMessage as FSInboundMessage, get_event_bus


def _convert_to_global_inbound(fs_msg: FSInboundMessage):
    """Convert a future_subagent InboundMessage to a global type.bus.InboundMessage.

    Both dataclasses have identical shape — simple field copy.
    """
    from type.bus import InboundMessage

    return InboundMessage(
        channel=fs_msg.channel,
        sender_id=fs_msg.sender_id,
        chat_id=fs_msg.chat_id,
        content=fs_msg.content,
        timestamp=fs_msg.timestamp,
        media=fs_msg.media,
        metadata=fs_msg.metadata,
        session_id=fs_msg.session_id,
    )


def _get_message_bus():
    """Lazily resolve the global MessageBus singleton."""
    from bus import MessageBus
    from channels import channel_manager
    bus = channel_manager.get_bus()
    if bus is not None:
        return bus
    return MessageBus()


# ── Bridge state ────────────────────────────────────────────────────────

_bridge_task: asyncio.Task | None = None
_running = False


async def _consume_loop() -> None:
    """Background loop: consume from EventBus → forward to MessageBus."""
    event_bus = get_event_bus()
    message_bus = _get_message_bus()
    logger.info("EventBusBridge consume loop started")

    while True:
        try:
            fs_msg: FSInboundMessage = await event_bus.consume()

            injected_event = fs_msg.metadata.get("injected_event", "unknown")
            logger.info(
                "EventBusBridge: forwarding event={} session_id={} content_len={}",
                injected_event,
                fs_msg.session_id,
                len(fs_msg.content),
            )

            global_msg = _convert_to_global_inbound(fs_msg)
            await message_bus.publish_inbound(global_msg)

        except asyncio.CancelledError:
            logger.info("EventBusBridge consume loop cancelled")
            break
        except Exception as e:
            logger.error("EventBusBridge consume loop error: {}", e)
            await asyncio.sleep(1.0)


def start_bridge() -> None:
    """Start the bridge consume loop on the running asyncio event loop.

    Called by ``init_registry()`` during service startup.
    No-op if the bridge is already running or no event loop exists.
    """
    global _bridge_task, _running

    if _running or _bridge_task is not None:
        logger.debug("EventBusBridge already running, skipping start")
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("EventBusBridge: no running event loop, deferring start")
        return

    _running = True
    _bridge_task = loop.create_task(_consume_loop())
    logger.info("EventBusBridge started on event loop")


def stop_bridge() -> None:
    """Cancel the bridge consume loop. Called at service shutdown."""
    global _bridge_task, _running

    if _bridge_task and not _bridge_task.done():
        _bridge_task.cancel()
    _bridge_task = None
    _running = False
    logger.info("EventBusBridge stopped")


def is_bridge_running() -> bool:
    """Return whether the bridge consume loop is active."""
    return _running
