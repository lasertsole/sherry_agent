import json
from typing import Any
from threading import Thread
from channels import channel_manager
from subagent import subagent_manager
from runtime import relation_register
from bus import InboundMessage, OutboundMessage

"""Subagent notification handler"""
async def process_subagent_notify(msg: InboundMessage):
    session_id: str = msg.session_id
    result_content: str = msg.content

    # If session is associated with a channel, send the result to the channel recipient
    if channel_chat_id:= relation_register.get_channel_chat_id_by_session_id(session_id):
        channel_id, chat_id = channel_chat_id

        channel = channel_manager.get_channel(channel_id)
        if channel:
            await channel.send(OutboundMessage(channel=channel_id, chat_id=chat_id, content=result_content))
    # If session is associated with a websocket, send the result to the websocket recipient
    elif websocket:= relation_register.get_websocket_by_session_id(session_id):
        res:dict[str, Any] = {"event": "notification", "content": result_content}
        await websocket.send_text(json.dumps(res))

"""End subagent notification handler"""
def run() -> None:
    event_loop = subagent_manager.get_event_loop()
    subagent_manager.set_consumer(process_subagent_notify)
    subagent_manager.start_service()

    try:
        event_loop.run_forever()
    except Exception:
        pass

subagent_thread: Thread = Thread(target=run, daemon=True)
subagent_thread.start()