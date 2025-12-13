import asyncio
from threading import Thread
from cron import cron_service
from typing import AsyncGenerator
from type import MultiModalMessage
from runtime import relation_register
from heartbeat import heartbeat_service
from server.service import async_generate
from bus import InboundMessage, OutboundMessage
from channels import BaseChannel, channel_manager
from ..service import process_heartbeat_task, process_heartbeat_notify
from pub_func import string_to_unique_int, process_sse_data, check_if_image_and_convert_to_base64


"""以下是常规处理频道信息"""
async def process_inbound(message: InboundMessage, channel: BaseChannel) -> None:
    user_input_text: str = message.content
    user_input_media: list[str] = getattr(message, "media", [])

    # 处理多媒体
    image_base64_list: list[str] = []
    for url in user_input_media:

        # 判断是否为图片
        is_img, content_type, fmt, base64_str = check_if_image_and_convert_to_base64(url)
        if is_img and base64_str:
            image_base64_list.append(base64_str)

    user_input: MultiModalMessage = MultiModalMessage(text = user_input_text, image_base64_list = image_base64_list)

    # 会话ID只与平台名有关
    session_id:str = str(string_to_unique_int(channel.name))

    # 注册频道会话(幂等操作)
    relation_register.register_channel_chat(session_id=session_id, channel_id=channel.name, chat_id=message.chat_id)

    ai_reply: str = ""
    stream: AsyncGenerator[str, None] = async_generate(session_id = session_id, multi_modal_message = user_input, is_stream = False)
    async for item in stream:
        ai_reply += process_sse_data(item)

    await channel.send(OutboundMessage(channel=channel.name, chat_id = message.chat_id, content = ai_reply))

# 设置频道的InboundMessage消费者
channel_manager.set_inbound_consumer(process_inbound)

async def process_outbound(message: OutboundMessage, channel: BaseChannel) -> None:
    session_id:str = str(string_to_unique_int(channel.name))

    # 注册频道会话(幂等操作)
    relation_register.register_channel_chat(session_id=session_id, channel_id=channel.name, chat_id=message.chat_id)

# 设置频道的OutboundMessage消费者
channel_manager.set_outbound_consumer(process_outbound)
"""以上是常规处理频道信息"""

"""以下是处理心跳事件"""
async def _process_heartbeat_task(task: str) -> str:
    return await process_heartbeat_task(task=task)

heartbeat_service.on_execute = _process_heartbeat_task

async def _process_heartbeat_notify(agent_res: str) -> None:
    return await process_heartbeat_notify(agent_res)

heartbeat_service.on_notify = _process_heartbeat_notify
"""以上是处理心跳事件"""

def run() -> None:
    # 从频道管理器获取事件循环，让 心跳服务 和 cron服务 运行在相同的事件循环中
    event_loop = channel_manager.get_event_loop()

    # 启动心跳服务
    asyncio.run_coroutine_threadsafe(heartbeat_service.start(), event_loop)
    # 启动 cron 服务
    asyncio.run_coroutine_threadsafe(cron_service.start(), event_loop)
    # 启动频道管理器（内部会调用 run_forever）
    channel_manager.start_service()

    try:
        event_loop.run_forever()
    except Exception:
        pass


channel_thread: Thread = Thread(target=run, daemon=True)
channel_thread.start()