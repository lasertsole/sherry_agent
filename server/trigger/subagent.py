import json
from typing import Any
from threading import Thread
from channels import channel_manager
from subagent import subagent_manager
from runtime import relation_register
from bus import InboundMessage, OutboundMessage

"""以下是处理subagent"""
async def process_subagent_notify(msg: InboundMessage):
    session_id: str = msg.session_id
    result_content: str = msg.content

    # 如果session与频道关联，则将结果信息发回给频道接受者
    if channel_chat_id:= relation_register.get_channel_chat_id_by_session_id(session_id):
        channel_id, chat_id = channel_chat_id

        channel = channel_manager.get_channel(channel_id)
        if channel:
            await channel.send(OutboundMessage(channel=channel_id, chat_id=chat_id, content=result_content))
    # 如果session与websocket关联，则将结果信息发回给websocket接受者
    elif websocket:= relation_register.get_websocket_by_session_id(session_id):
        res:dict[str, Any] = {"event": "notification", "content": result_content}
        await websocket.send_text(json.dumps(res))

"""以上是处理subagent"""

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