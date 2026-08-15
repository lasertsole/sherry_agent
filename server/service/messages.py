import time
import base64
import asyncio
import json
from loguru import logger
from agent import built_agent
from typing import AsyncGenerator, Any
from runtime import state_register_mem
from type.message import MultiModalMessage
from langchain.messages import AIMessageChunk
from pub_func import build_agent_config, is_url
from ..DAO import clear_session as clear_session_DAO
from context_engine.curator import reset_idle_for_seconds
from agent.middlewares.heartbeat_staleness import HeartbeatTimeoutError
from context_engine import get_history_by_turn_page as _get_history_by_turn_page
from context_engine import get_session_ids
from langchain_core.messages import HumanMessage, BaseMessage, ToolCall, ToolCallChunk, ToolMessage
from langgraph.types import Command


# Stash of pending tool args, keyed by tool_call_id. Populated at tool_start
# time and consumed when the matching ToolMessage arrives in "updates" mode.
_pending_args: dict[str, dict] = {}


def _normalize_args(args) -> dict:
    """Normalize ToolCall.args (dict OR JSON string) into a plain dict."""
    if isinstance(args, str):
        try:
            return json.loads(args)
        except Exception:
            return {"raw": args}
    if not isinstance(args, dict):
        return {"raw": str(args)}
    return args


def _normalize_text(content) -> str:
    """Normalize ToolMessage.content (str OR list of content blocks) into str."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(content)


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
        return agent.astream(input=input_dict, config=build_agent_config(session_id), stream_mode=["messages", "updates"])
    else:
        return agent.ainvoke(input=input_dict, config=build_agent_config(session_id))

"""End agent assembly logic"""

"""Response generation logic — yields typed dict chunks

Each yielded item is a dict ``{"type": <str>, "content": <str>}`` where
``type`` is one of:
- ``"text"``       — conversational text fragment
- ``"tool_start"`` — a tool invocation begins (content = tool name)
- ``"tool_end"``   — a tool invocation completes (content = tool name)

Callers that only need the plain text (e.g. channel consumers) can
join ``chunk["content"]`` for every item.
"""
async def async_generate(session_id: str, multi_modal_message: MultiModalMessage, is_stream: bool = True)-> AsyncGenerator[dict[str, str], None]:
    start_time = time.time()
    logger.debug(
        f"Agent execution started: session_id={session_id}, is_stream={is_stream}, "
        f"input_text_length={len(multi_modal_message.text) if multi_modal_message.text else 0}"
    )

    # Create the agent with assembled context
    ai_text:str = ""

    # reset curator reset_idle_for_seconds
    reset_idle_for_seconds()

    # Control answering
    state_register_mem.set_state(session_id, "answering", True)

    generator = None

    try:
        if is_stream:
            # Stream directly from the context-assembled agent
            generator = await _get_generator(session_id, multi_modal_message)
            async for chunk in generator:
                # With stream_mode=["messages", "updates"], each chunk is (mode, data).
                # Only process the "messages" mode; skip "updates" mode chunks.
                if state_register_mem.get_state(session_id, "answering") == False:
                    raise asyncio.CancelledError

                mode: str = chunk[0]
                data: Any = chunk[1]
                if mode == "updates":
                    for node_name, state_update in data.items():
                        if node_name != "tools":
                            continue
                        msgs = state_update.get("messages", [])
                        for tm in msgs:
                            if not isinstance(tm, ToolMessage):
                                continue
                            tool_id = tm.tool_call_id
                            name = state_register_mem.get_state(session_id, "current_tool_name", "")
                            args = _pending_args.pop(tool_id, {})
                            yield {
                                "type": "tool_result",
                                "content": _normalize_text(tm.content),
                                "tool_id": tool_id,
                                "tool_name": name,
                                "args": args,
                                "error": bool(getattr(tm, "status", None) == "error"),
                            }
                            # Robust tool_end: emitted here on the REAL ToolMessage,
                            # independent of whether the model emitted adjacent text.
                            # Image/vision tools often produce only a tool call chunk
                            # with no text, so the old messages-mode gating on
                            # `msg_chunk.content` never fired, leaving the card
                            # permanently "running" (the stuck-tool bug).
                            yield {"type": "tool_end", "content": name}
                            state_register_mem.set_state(session_id, "current_tool_id", "")
                    continue
                if mode != "messages":
                    continue

                # For "messages" mode, data is (message_chunk, metadata_dict).
                msg_chunk: BaseMessage = data[0]
                metadata: dict[str, Any] = data[1]

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
                            tool_name = state_register_mem.get_state(session_id, "current_tool_name", "")
                            _pending_args[tool_call["id"]] = _normalize_args(tool_call.get("args"))
                            ai_text += f"\n\n**Calling tool {tool_name}...**"
                            yield {
                                "type": "tool_start",
                                "content": tool_name,
                                "args": _pending_args[tool_call["id"]],
                            }

                    # NOTE: tool_end is now emitted from the updates-mode "tools"
                    # branch (on the real ToolMessage), so a tool that produces no
                    # adjacent text still gets a completion signal. The old
                    # messages-mode implementation gated on msg_chunk.content, which
                    # is why image/vision tools (usually text-free) hung forever.
                    # End tool call output logic

                    # Conversation output logic
                    if len(msg_chunk.content) > 0:
                        res: str = msg_chunk.content
                        ai_text += res
                        yield {"type": "text", "content": res}
                    # End conversation output logic

        else:
            generator = await _get_generator(session_id, multi_modal_message, is_stream = False)
            result: dict[str, Any] = await generator
            res: str = result["messages"][-1].content
            ai_text += res
            yield {"type": "text", "content": res}

        elapsed = time.time() - start_time
        logger.debug(
            f"Agent execution completed: session_id={session_id}, duration={elapsed:.2f}s, "
            f"output_length={len(ai_text)}"
        )
    except asyncio.CancelledError:
        elapsed = time.time() - start_time
        yield {"type": "text", "content": "Request cancelled"}
        logger.debug(
            f"Agent execution cancelled: session_id={session_id}, duration={elapsed:.2f}s"
        )
    except HeartbeatTimeoutError as e:
        elapsed = time.time() - start_time
        yield {"type": "text", "content": "\n\n**[Heartbeat Timeout]** Agent idle timeout exceeded — automatically terminated."}
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
        _pending_args.clear()

"""HITL interrupt detection — checks agent state for pending interrupts.

When the HumanInTheLoop middleware calls ``interrupt()``, the agent stream
ends and the interrupt payload is stored in the graph state's ``tasks``.
This function inspects the state and returns the interrupt request so
the WebSocket layer can forward it to the client for human approval.
"""
async def get_pending_interrupt(session_id: str) -> dict[str, Any] | None:
    """Return the pending HITL interrupt payload for a session, or ``None``.

    The returned dict has the shape::

        {
            "tool_name": str,
            "tool_args": dict,
            "description": str,
            "allowed_decisions": list[str],
        }
    """
    try:
        agent = await built_agent()
        if agent is None:
            return None
        config = build_agent_config(session_id)
        state = await agent.aget_state(config=config)

        for task in getattr(state, "tasks", []):
            if hasattr(task, "interrupts") and task.interrupts:
                for intr in task.interrupts:
                    value = getattr(intr, "value", None)
                    if value is None:
                        continue
                    action_requests = value.get("action_requests", []) if isinstance(value, dict) else []
                    review_configs = value.get("review_configs", []) if isinstance(value, dict) else []
                    if not action_requests:
                        continue
                    ar = action_requests[0]
                    rc = review_configs[0] if review_configs else {}
                    return {
                        "tool_name": ar.get("name", "unknown"),
                        "tool_args": ar.get("args", {}),
                        "description": ar.get("description", ""),
                        "allowed_decisions": rc.get("allowed_decisions", ["approve", "reject"]),
                    }
            break
        return None
    except Exception as e:
        logger.debug(f"get_pending_interrupt failed for session_id={session_id}: {e}")
        return None
"""End HITL interrupt detection"""

"""HITL resume — continues the agent after a human decision.

Called when the client sends back an approval/rejection. Uses
``Command(resume=...)`` to un-pause the graph and streams the
remaining output just like ``async_generate``.
"""
async def resume_agent(
    session_id: str,
    decision: str,
    message: str = "",
    edited_args: dict[str, Any] | None = None,
) -> AsyncGenerator[dict[str, str], None]:
    """Resume the agent after a HITL interrupt.

    Args:
        session_id:  Active session ID.
        decision:    ``"approve"``, ``"reject"``, or ``"edit"``.
        message:     Optional user message accompanying the decision.
        edited_args: When ``decision == "edit"``, the new tool arguments.

    Yields:
        Same chunk format as :func:`async_generate`.
    """
    start_time = time.time()
    logger.info(
        f"Agent resume started: session_id={session_id}, decision={decision}"
    )

    agent = await built_agent()
    config = build_agent_config(session_id)

    # Inject session_id into the resume value. On a normal turn it arrives via the
    # graph input dict (see _get_generator), but Command(resume=...) merges only the
    # resume value into state — without session_id here, MultimodalProcessor's
    # _before_agent_impl would raise "Not pass session_id" on resume.
    resume_value: dict[str, Any] = {
        "session_id": session_id,
        "decisions": [{"type": decision, "message": message}],
    }
    if decision == "edit" and edited_args is not None:
        resume_value["decisions"][0]["edited_action"] = {"args": edited_args}

    state_register_mem.set_state(session_id, "answering", True)

    try:
        async for chunk in agent.astream(
            Command(resume=resume_value),
            config=config,
            stream_mode=["messages", "updates"],
        ):
            if state_register_mem.get_state(session_id, "answering") == False:
                raise asyncio.CancelledError

            mode: str = chunk[0]
            data: Any = chunk[1]
            if mode == "updates":
                for node_name, state_update in data.items():
                    if node_name != "tools":
                        continue
                    msgs = state_update.get("messages", [])
                    for tm in msgs:
                        if not isinstance(tm, ToolMessage):
                            continue
                        tool_id = tm.tool_call_id
                        name = state_register_mem.get_state(session_id, "current_tool_name", "")
                        args = _pending_args.pop(tool_id, {})
                        yield {
                            "type": "tool_result",
                            "content": _normalize_text(tm.content),
                            "tool_id": tool_id,
                            "tool_name": name,
                            "args": args,
                            "error": bool(getattr(tm, "status", None) == "error"),
                        }
                        # Robust tool_end — see note in async_generate. Guarantees a
                        # completion signal even when the tool chunk carries no text.
                        yield {"type": "tool_end", "content": name}
                        state_register_mem.set_state(session_id, "current_tool_id", "")
                continue
            if mode != "messages":
                continue

            msg_chunk: BaseMessage = data[0]
            metadata: dict[str, Any] = data[1]

            if metadata.get("langgraph_node", None) != "model" or metadata.get("lc_source") == "summarization":
                continue

            if isinstance(msg_chunk, AIMessageChunk):
                tool_calls = msg_chunk.tool_calls if msg_chunk.tool_calls and len(msg_chunk.tool_calls) > 0 else msg_chunk.tool_call_chunks
                if len(tool_calls) > 0 or state_register_mem.get_state(session_id, "current_tool_id", "").strip():
                    repeat_flag = True
                    if len(tool_calls) > 0:
                        tool_call = tool_calls[0]
                        if tool_call["name"]:
                            if tool_call["name"].strip() or tool_call["name"].strip() != state_register_mem.get_state(session_id, "current_tool_name"):
                                state_register_mem.set_state(session_id, "current_tool_name", tool_call['name'])
                        if tool_call["id"]:
                            if tool_call["id"].strip() or tool_call["id"].strip() != state_register_mem.get_state(session_id, "current_tool_id"):
                                state_register_mem.set_state(session_id, "current_tool_id", tool_call['id'])
                                repeat_flag = False
                    if not repeat_flag:
                        tool_name = state_register_mem.get_state(session_id, "current_tool_name", "")
                        _pending_args[tool_call["id"]] = _normalize_args(tool_call.get("args"))
                        yield {
                            "type": "tool_start",
                            "content": tool_name,
                            "args": _pending_args[tool_call["id"]],
                        }

                    # NOTE: tool_end handled in the updates-mode "tools" branch above
                    # (mirrors async_generate). Removed the old messages-mode gating
                    # on msg_chunk.content so text-free vision/image tools complete.

                if len(msg_chunk.content) > 0:
                    yield {"type": "text", "content": msg_chunk.content}

        elapsed = time.time() - start_time
        logger.debug(
            f"Agent resume completed: session_id={session_id}, duration={elapsed:.2f}s"
        )
    except asyncio.CancelledError:
        yield {"type": "text", "content": "Request cancelled"}
        logger.debug(f"Agent resume cancelled: session_id={session_id}")
    except HeartbeatTimeoutError as e:
        yield {"type": "text", "content": "\n\n**[Heartbeat Timeout]** Agent idle timeout exceeded — automatically terminated."}
        logger.warning(f"Agent resume heartbeat timeout: session_id={session_id}, error={e}")
    except Exception as e:
        logger.error(f"Agent resume failed: session_id={session_id}, error={str(e)}")
        logger.exception(e)
        raise e
    finally:
        state_register_mem.set_state(session_id, "current_tool_name", "")
        state_register_mem.set_state(session_id, "current_tool_id", "")
        state_register_mem.set_state(session_id, "answering", False)
        _pending_args.clear()
"""End HITL resume"""

"""End response generation logic"""

"""History retrieval logic"""
def get_history_by_turn_page(session_id: str, min_turn_num: int, turn_page_size: int, turn_page_num: int) -> list[dict[str, Any]]:
    return _get_history_by_turn_page(session_id, min_turn_num, turn_page_size, turn_page_num)
"""End history retrieval logic"""

"""Session list retrieval logic"""
def get_session_list() -> list[dict[str, Any]]:
    """Enumerate all distinct sessions, newest activity first.

    Returns a list of
    ``{"session_id": str, "last_time": str, "title": str}`` dicts.
    """
    return get_session_ids()
"""End session list retrieval logic"""

"""Clear session history logic"""
async def clear_session(session_id: str):
    logger.debug(f"Clearing session history: session_id={session_id}")
    await clear_session_DAO(session_id = session_id)
    logger.debug(f"Session history cleared: session_id={session_id}")
"""End clear session history logic"""