"""EventBus → runtime delivery bridge.

Single consumer of the sub-agent EventBus. For each delivery message
published via ``get_event_bus().publish_internal()``, the bridge resolves
the target session to either a channel chat (QQ/etc.) or a websocket client
and pushes the message that it directly — no longer forwarding through the
project-wide ``MessageBus``.

Routing (mirrors the legacy ``server/trigger/subagent/core.py`` consumer):
    1. session → channel chat ``(channel_id, chat_id)`` → ``channel.send(OutboundMessage)``
    2. else session → websocket → ``websocket.send_text(JSON)``
    3. else no target → log-and-drop (never raise)

Internal (sub→sub) injections are NOT routed to any user channel/websocket;
they are consumed and discarded here by design (``internal=True``).

Lifecycle:
    - ``start_bridge()`` — called by the initial EventBus consumer wiring at service startup
    - ``stop_bridge()``  — called at shutdown
    - The consume loop runs on the current asyncio event loop
"""

from __future__ import annotations

import asyncio
import json
from loguru import logger

from .core import InboundMessage as FSInboundMessage, get_event_bus


# ── Bridge state ────────────────────────────────────────────────────────

_bridge_task: "asyncio.Task[None] | None" = None
_running = False


def _strip_session_prefix(session_id: str | None) -> str | None:
    """Strip the ``agent:main:session:`` prefix to a bare session id for registry lookup.

    The sub-agent EventBus carries prefixed session keys (``agent:main:session:{id}``)
    while ``relation_register`` stores bare session ids — so we normalize before lookup.
    """
    if not session_id:
        return None
    prefix = "agent:main:session:"
    if session_id.startswith(prefix):
        return session_id[len(prefix) :]
    return session_id


async def _deliver_to_channel(channel_id: str, chat_id: str, content: str) -> None:
    """Send a delivery message to a channel chat recipient."""
    from channels.manager import channel_manager
    from type.bus import OutboundMessage

    channel = channel_manager.get_channel(channel_id)
    if channel is None:
        logger.warning("EventBusBridge: channel {} not found for delivery", channel_id)
        return
    await channel.send(OutboundMessage(channel=channel_id, chat_id=chat_id, content=content))
    logger.info(
        "EventBusBridge: delivered to channel={} chat_id={} content_len={}",
        channel_id,
        chat_id,
        len(content),
    )


async def _deliver_to_websocket(session_id: str, content: str) -> None:
    """Send a delivery message to the websocket recipient bound to a session."""
    from runtime import relation_register

    websocket = relation_register.get_websocket_by_session_id(session_id)
    if websocket is None:
        logger.warning("EventBusBridge: no websocket for session {}", session_id)
        return
    payload = {"event": "notification", "content": content}
    await websocket.send_text(json.dumps(payload))
    logger.info(
        "EventBusBridge: delivered to websocket for session={} content_len={}",
        session_id,
        len(content),
    )


async def _route_delivery(fs_msg: FSInboundMessage) -> None:
    """Route a single delivery message to its target (channel or websocket).

    Never raises for a missing target — logs and drops. Returns after routing.
    """
    # Sub→sub internal injections must NOT reach any user channel/websocket.
    if fs_msg.metadata.get("internal"):
        logger.debug(
            "EventBusBridge: discarding internal sub→sub injection (session={})", fs_msg.session_id
        )
        return

    bare_session = _strip_session_prefix(fs_msg.session_id)
    if not bare_session:
        logger.warning(
            "EventBusBridge: message has no session_id, dropping: {}", fs_msg.content[:80]
        )
        return

    from runtime import relation_register

    pair = relation_register.get_channel_chat_id_by_session_id(bare_session)
    if pair:
        channel_id, chat_id = pair
        await _deliver_to_channel(channel_id, chat_id, fs_msg.content)
        return

    await _deliver_to_websocket(bare_session, fs_msg.content)


async def _consume_loop() -> None:
    """Background loop: consume from EventBus → route to channel/websocket."""
    event_bus = get_event_bus()
    logger.info("EventBusBridge consume loop started")

    while True:
        try:
            fs_msg: FSInboundMessage = await event_bus.consume()

            injected_event: str = str(fs_msg.metadata.get("injected_event") or "unknown")
            logger.info(
                "EventBusBridge: routing event={} session_id={} content_len={}",
                injected_event,
                fs_msg.session_id,
                len(fs_msg.content),
            )

            await _route_delivery(fs_msg)

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
        _ = _bridge_task.cancel()
    _bridge_task = None
    _running = False
    logger.info("EventBusBridge stopped")


def is_bridge_running() -> bool:
    """Return whether the bridge consume loop is active."""
    return _running
