import time
import base64
import asyncio
from loguru import logger
from robyn import SSEMessage
from agent import built_agent
from config import ASSISTANT_NAME
from typing import AsyncGenerator, Any
from runtime import state_register_mem
from type.message import MultiModalMessage
from langchain.messages import AIMessageChunk
from pub_func import build_agent_config, is_url
from ..DAO import clear_session as clear_session_DAO
from context_engine import get_history_by_page as _get_history_by_page
from agent.middlewares.heartbeat_staleness import HeartbeatTimeoutError
from langchain_core.messages import HumanMessage, BaseMessage, ToolCall, ToolCallChunk


async def _get_agent_history_list(session_id: str)-> list[BaseMessage]:
    agent = await built_agent()

    state = await agent.aget_state(config=build_agent_config(session_id))
    return state.values.get("messages", [])

def _get_content_list(multi_modal_message: MultiModalMessage)-> list[dict[str, str]]:
    user_text: str = multi_modal_message.text
    content_list: list[dict[str, Any]] = [{"type": "text", "text": user_text}]

    ##** Image handling logic **##
    if multi_modal_message.image_path_list:
        for image_path in multi_modal_message.image_path_list:
            if is_url(image_path):
                content_list.append({"type": "image_url", "image_url": {"url": image_path}})
            else:
                logger.warning(f"Image path is not a URL: {image_path}")

    if multi_modal_message.image_base64_list:
        for image_base64 in multi_modal_message.image_base64_list:
            # Check if it already has a data URI prefix
            if image_base64.startswith('data:image/'):
                # Already has the prefix, use as-is
                image_url = image_base64
            else:
                # No prefix, add one
                image_url = f"data:image/png;base64,{image_base64}"

            content_list.append({"type": "image_url", "image_url": {"url": image_url}})

    if multi_modal_message.image_bytes_list:
        for image_bytes in multi_modal_message.image_bytes_list:
            base64_str = base64.b64encode(image_bytes).decode('utf-8')
            image_url = f"data:image/png;base64,{base64_str}"
            content_list.append({"type": "image_url", "image_url": {"url": image_url}})
    ##** End image handling logic **##

    ##** Audio handling logic **##
    if multi_modal_message.audio_path_list:
        for audio_path in multi_modal_message.audio_path_list:
            if is_url(audio_path):
                content_list.append({"type": "audio_url", "audio_url": {"url": audio_path}})
            else:
                logger.warning(f"Image path is not a URL: {audio_path}")

    if multi_modal_message.audio_bytes_list:
        for audio_bytes in multi_modal_message.audio_bytes_list:
            content_list.append({"type": "audio_bytes", "audio_bytes": {"bytes": audio_bytes}})
    ##** End audio handling logic **##

    ##** Video handling logic **##
    if multi_modal_message.video_path_list:
        for video_path in multi_modal_message.video_path_list:
            if is_url(video_path):
                content_list.append({"type": "video_url", "video_url": {"url": video_path}})
            else:
                logger.warning(f"Image path is not a URL: {video_path}")

    if multi_modal_message.video_bytes_list:
        for video_bytes in multi_modal_message.video_bytes_list:
            content_list.append({"type": "video_bytes", "video_bytes": {"bytes": video_bytes}})
    ##** End video handling logic **##

    return content_list

"""Agent assembly logic — builds agent with context"""
async def _get_generator(session_id: str, multi_modal_message: MultiModalMessage, is_stream: bool = True):
    start_time = time.time()

    logger.debug(
        f"Building agent: session_id={session_id}"
    )

    # Create the agent
    agent = await built_agent()

    # Prepare the content_list
    content_list:list[dict[str, str]] = _get_content_list(multi_modal_message)
            
    elapsed = time.time() - start_time
    logger.debug(
        f"Agent generator prepared: session_id={session_id}, duration={elapsed:.2f}s, "
        f"is_stream={is_stream}, has_images={len(multi_modal_message.image_base64_list) if multi_modal_message.image_base64_list else 0}"
    )

    input_dict = {"session_id": session_id, "messages": [HumanMessage(content=content_list)]}
    if is_stream:
        return agent.astream(input=input_dict, config=build_agent_config(session_id), stream_mode="messages")
    else:
        return agent.ainvoke(input=input_dict, config=build_agent_config(session_id))

"""End agent assembly logic"""

"""Response generation logic — yields SSE messages"""
async def async_generate(session_id: str, multi_modal_message: MultiModalMessage, is_stream: bool = True)-> AsyncGenerator[str, None]:
    start_time = time.time()
    logger.debug(
        f"Agent execution started: session_id={session_id}, is_stream={is_stream}, "
        f"input_text_length={len(multi_modal_message.text) if multi_modal_message.text else 0}"
    )

    # Create the agent with assembled context
    ai_text:str = ""

    # Control answering
    state_register_mem.set_state(session_id, "answering", True)

    generator = None

    try:
        yield SSEMessage(f"{ASSISTANT_NAME}:")

        if is_stream:
            # Stream directly from the context-assembled agent
            generator = await _get_generator(session_id, multi_modal_message)
            async for chunk in generator:
                msg_chunk: BaseMessage = chunk[0]
                metadata: dict[str, Any] = chunk[1]

                # Filter out outputs from non-model nodes in the lifecycle
                if metadata.get("langgraph_node", None) != "model" or metadata.get("lc_source") == "summarization":
                    continue

                if isinstance(msg_chunk, AIMessageChunk):
                    # Tool call output logic
                    tool_calls: list[ToolCall] | list[ToolCallChunk] = msg_chunk.tool_calls if msg_chunk.tool_calls and len(
                        msg_chunk.tool_calls) > 0 else msg_chunk.tool_call_chunks
                    if len(tool_calls) > 0 or state_register_mem.get_state(session_id, "current_tool_id", "").strip():
                        repeat_flag: bool = True  # Prevent duplicate tool call output
                        if len(tool_calls) > 0:
                            tool_call = tool_calls[0]

                            if tool_call["name"]:
                                if tool_call["name"].strip() or tool_call["name"].strip() != state_register_mem.get_state(session_id, "current_tool_name"):
                                    state_register_mem.set_state(session_id, "current_tool_name", tool_call['name'])

                            if tool_call["id"]:
                                if tool_call["id"].strip() or tool_call["id"].strip() != state_register_mem.get_state(session_id, "current_tool_id"):
                                    state_register_mem.set_state(session_id,"current_tool_id", tool_call['id'])
                                    repeat_flag = False

                        if not repeat_flag:
                            res: str = f"\n\n**Calling tool {state_register_mem.get_state(session_id, "current_tool_name", "")}...**"
                            ai_text += res
                            yield SSEMessage(res)

                    if state_register_mem.get_state(session_id, "current_tool_id", "").strip() and msg_chunk.content is not None and msg_chunk.content:
                        res: str = f"\n\n**Tool {state_register_mem.get_state(session_id, "current_tool_name", "")} completed.**\n\n"
                        ai_text += res
                        yield SSEMessage(res)
                        state_register_mem.set_state(session_id, "current_tool_id", "")
                    # End tool call output logic

                    # Conversation output logic
                    if len(msg_chunk.content) > 0:
                        res: str = msg_chunk.content
                        ai_text += res
                        yield SSEMessage(res)
                    # End conversation output logic

        else:
            generator = await _get_generator(session_id, multi_modal_message, is_stream = False)
            result: dict[str, Any] = await generator
            res: str = result["messages"][-1].content
            ai_text += res
            yield SSEMessage(res)

        elapsed = time.time() - start_time
        logger.debug(
            f"Agent execution completed: session_id={session_id}, duration={elapsed:.2f}s, "
            f"output_length={len(ai_text)}"
        )
    except asyncio.CancelledError:
        elapsed = time.time() - start_time
        yield SSEMessage("Request cancelled")
        logger.debug(
            f"Agent execution cancelled: session_id={session_id}, duration={elapsed:.2f}s"
        )
    except HeartbeatTimeoutError as e:
        elapsed = time.time() - start_time
        yield SSEMessage(f"\n\n**[Heartbeat Timeout]** Agent idle timeout exceeded — automatically terminated.")
        logger.warning(
            f"Agent heartbeat timeout: session_id={session_id}, duration={elapsed:.2f}s, "
            f"error={e}"
        )
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(
            f"Agent execution failed: session_id={session_id}, duration={elapsed:.2f}s, "
            f"error={str(e)}"
        )
        logger.exception(e)
        raise e
    finally:
        # Gracefully close the async generator to avoid GeneratorExit/RuntimeError
        if generator is not None and is_stream:
            try:
                await generator.aclose()
            except Exception:
                pass  # GeneratorExit is expected and harmless
        # Reset tool tracking state
        state_register_mem.set_state(session_id, "current_tool_name", "")
        state_register_mem.set_state(session_id, "current_tool_id", "")
        state_register_mem.set_state(session_id, "answering", False)

"""End response generation logic"""

"""History retrieval logic"""
def get_history_by_page(session_id: str, min_turn_num: int, turn_page_size: int, turn_page_num: int) -> list[dict[str, Any]]:
    return _get_history_by_page(session_id, min_turn_num, turn_page_size, turn_page_num)
"""End history retrieval logic"""

"""Clear session history logic"""
async def clear_session(session_id: str):
    logger.debug(f"Clearing session history: session_id={session_id}")
    await clear_session_DAO(session_id = session_id)
    logger.debug(f"Session history cleared: session_id={session_id}")
"""End clear session history logic"""