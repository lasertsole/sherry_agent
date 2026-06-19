import asyncio
from threading import Thread
from typing import AsyncGenerator
from type import MultiModalMessage
from runtime import relation_register
from server.service import async_generate
from bus import InboundMessage, OutboundMessage
from channels import BaseChannel, channel_manager
from skills.builtin.core.heartbeat import heartbeat_service
from server.service import process_heartbeat_task, process_heartbeat_notify
from pub_func import string_to_unique_int, process_sse_data, check_if_image_and_convert_to_base64

"""Channel inbound message handler"""
async def _process_inbound(message: InboundMessage, channel: BaseChannel) -> None:
    user_input_text: str = message.content
    user_input_media: list[str] = getattr(message, "media", [])

    # Process media attachments
    image_base64_list: list[str] = []
    for url in user_input_media:

        # Check if URL points to an image
        is_img, content_type, fmt, base64_str = check_if_image_and_convert_to_base64(url)
        if is_img and base64_str:
            image_base64_list.append(base64_str)

    user_input: MultiModalMessage = MultiModalMessage(text = user_input_text, image_base64_list = image_base64_list)

    # Session ID is derived from the channel name only
    session_id:str = str(string_to_unique_int(channel.name))

    # Register channel session (idempotent)
    relation_register.register_channel_chat(session_id=session_id, channel_id=channel.name, chat_id=message.chat_id)

    ai_reply: str = ""
    stream: AsyncGenerator[str, None] = async_generate(session_id = session_id, multi_modal_message = user_input, is_stream = False)
    async for item in stream:
        ai_reply += process_sse_data(item)

    await channel.send(OutboundMessage(channel=channel.name, chat_id = message.chat_id, content = ai_reply))

# Set channel inbound consumer
channel_manager.set_inbound_consumer(_process_inbound)

async def _process_outbound(message: OutboundMessage, channel: BaseChannel) -> None:
    session_id:str = str(string_to_unique_int(channel.name))

    # Register channel session (idempotent)
    relation_register.register_channel_chat(session_id=session_id, channel_id=channel.name, chat_id=message.chat_id)

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