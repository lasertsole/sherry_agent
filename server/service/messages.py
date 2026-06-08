import requests
from loguru import logger
from robyn import SSEMessage
from config import ASSISTANT_NAME
from type import MultiModalMessage
from pub_func import build_agent_config
from typing import AsyncGenerator, Any, List
from langchain.messages import AIMessageChunk
from langgraph.graph.state import CompiledStateGraph
from ..DAO import clear_session as clear_session_DAO
from workspace.prompt_builder import build_system_prompt
from langgraph.checkpoint.base import BaseCheckpointSaver
from context_engine import rectification_and_standardization
from agent import built_agent, ModelType, build_async_sqlite_checkpointer
from langchain_core.messages import HumanMessage, BaseMessage, ToolCall, ToolCallChunk

def _get_agent_history_list(agent: CompiledStateGraph, session_id: str)-> List[BaseMessage]:
    return agent.get_state(config=build_agent_config(session_id)).values.get("messages", [])

"""以下是组装自带上下文的agent逻辑"""
async def _get_generator(session_id: str, multi_modal_message: MultiModalMessage, is_stream: bool = True):
    checkpointer: BaseCheckpointSaver = await build_async_sqlite_checkpointer()

    # 创建agent
    agent: CompiledStateGraph = built_agent(session_id = session_id, system_prompt=build_system_prompt(), checkpointer=checkpointer)

    user_text:str = multi_modal_message.text

    content_list:List[dict[str, str]] = [{"type": "text", "text": user_text}]
    if multi_modal_message.image_base64_list:
        for image_base64 in multi_modal_message.image_base64_list:
            # 判断是否已经有data URI前缀
            if image_base64.startswith('data:image/'):
                # 已有前缀，直接使用
                image_url = image_base64
            else:
                # 没有前缀，添加前缀
                image_url = f"data:image/png;base64,{image_base64}"

            content_list.append({"type": "image_url", "image_url": {"url": image_url}})
            # 切换模型
            agent = built_agent(system_prompt=build_system_prompt(), session_id = session_id, model_type = ModelType.VL_MODEL, enable_tool = False)

    if is_stream:
        return agent.astream(input={"messages": [HumanMessage(content = content_list)]}, config=build_agent_config(session_id), stream_mode="messages")
    else:
        return agent.ainvoke(input={"messages": [HumanMessage(content = content_list)]}, config=build_agent_config(session_id))

"""以上是组装自带上下文的agent逻辑"""

"""以下是返回信息逻辑"""
_current_tool_name: str = ""
_current_tool_id: str = ""
async def async_generate(session_id: str, multi_modal_message: MultiModalMessage, is_stream: bool = True)-> AsyncGenerator[str, None]:
    global _current_tool_name
    global _current_tool_id

    # 创建已经组装好上下文的agent
    ai_text:str = ""

    try:
        yield SSEMessage(f"{ASSISTANT_NAME}:")

        if is_stream:
            # 用已组装好上下文的agent直接输出
            generator = await _get_generator(session_id, multi_modal_message)
            async for chunk in generator:
                msg_chunk: BaseMessage = chunk[0]
                metadata: dict[str, Any] = chunk[1]

                # 过滤走生命周期中其他模型的输出
                if metadata.get("langgraph_node", None) != "model":
                    continue

                if isinstance(msg_chunk, AIMessageChunk):
                    # 以下是输出工具信息
                    tool_calls: List[ToolCall] | List[ToolCallChunk] = msg_chunk.tool_calls if msg_chunk.tool_calls and len(
                        msg_chunk.tool_calls) > 0 else msg_chunk.tool_call_chunks
                    if len(tool_calls) > 0 or _current_tool_id.strip():
                        repeat_flag: bool = True  # 防止重复输出工具信息
                        if len(tool_calls) > 0:
                            tool_call = tool_calls[0]

                            if tool_call["name"]:
                                if tool_call["name"].strip() or tool_call["name"].strip() != _current_tool_name:
                                    _current_tool_name = tool_call['name']

                            if tool_call["id"]:
                                if tool_call["id"].strip() or tool_call["id"].strip() != _current_tool_id:
                                    _current_tool_id = tool_call['id']
                                    repeat_flag = False

                        if not repeat_flag:
                            res: str = f"\n\n**调用工具 {_current_tool_name} 中**"
                            ai_text += res
                            yield SSEMessage(res)

                    if _current_tool_id and msg_chunk.content is not None and msg_chunk.content:
                        res: str = f"\n\n**调用工具 {_current_tool_name} 结束。**\n\n"
                        ai_text += res
                        yield SSEMessage(res)
                        _current_tool_id = ""
                    # 以上是输出工具信息

                    # 以下是对话信息
                    if len(msg_chunk.content) > 0:
                        res: str = msg_chunk.content
                        ai_text += res
                        yield SSEMessage(res)
                    # 以上是对话信息

        else:
            generator = await _get_generator(session_id, multi_modal_message, is_stream = False)
            result: dict[str, Any] = await generator
            res: str = result["messages"][-1].content
            ai_text += res
            yield SSEMessage(res)

    except requests.exceptions.HTTPError as e:
        yield SSEMessage(f"请求失败: {e.response.text}")
        logger.exception(e)
    except requests.exceptions.Timeout as e:
        yield SSEMessage(f"请求超时: {e.args[0]}")
        logger.exception(e)
    except Exception as e:
        logger.exception(e)
        raise e
    finally:
        # 重置工具信息
        _current_tool_name = ""
        _current_tool_id = ""


"""以上是返回信息逻辑"""

"""以下是会话结束逻辑"""
async def session_end(session_id: str):
    await rectification_and_standardization(session_id = session_id)
"""以上是会话结束逻辑"""

"""以下是清除会话历史记录"""
async def clear_session(session_id: str):
    await clear_session_DAO(session_id = session_id)
"""以上是清除会话历史记录"""