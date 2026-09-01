import time
import json
import base64
import asyncio
from loguru import logger
from agent import built_agent
from langgraph.types import Command
from typing import AsyncGenerator, Any
from runtime import state_register_mem
from context_engine import get_session_ids
from type.message import MultiModalMessage
from langchain.messages import AIMessageChunk
from pub_func import build_agent_config, is_url
from ..DAO import clear_session as clear_session_DAO
from context_engine.curator import reset_idle_for_seconds
from agent.middlewares.heartbeat_staleness import HeartbeatTimeoutError
from context_engine import get_history_by_turn_page as _get_history_by_turn_page
from langchain_core.messages import HumanMessage, BaseMessage, ToolCall, ToolCallChunk, ToolMessage


# Stash of pending tool args, keyed by [bare session id][tool_call_id] (the
# bare-id convention matches the answering flag). Populated at tool_start time
# and consumed when the matching ToolMessage arrives in "updates" mode.
# Per-session so concurrent sessions on separate WS connections never see each
# other's pending tool state.
_pending_args: dict[str, dict[str, dict]] = {}
# Raw JSON-fragment buffer per [bare session id][tool_id], accumulated until
# it parses to a dict.
_pending_raw: dict[str, dict[str, list[str]]] = {}


def _accumulate_pending_args(session_id: str, tool_id: str | None, raw_args) -> None:
    """Accumulate streamed ToolCall args fragments into the session's arg-bag.

    LangChain streams tool calls as a sequence of chunks: the first chunk carries
    the tool `id` with empty args, and subsequent (id-less) chunks carry the args
    as progressively-appended *partial JSON string fragments*. The old code waited
    for the first fragment to carry complete args, left the bag empty, so
    tool_start/tool_result both carried {} on the wire (and only after a page
    rebuild from the checkpointed final ToolCall did args appear).

    Buffering strategy:
    - A `str` fragment is APPENDED to a per-tool_id raw buffer; we then try to
      parse the whole buffer as JSON. As soon as it forms a non-empty dict the
      buffer is atomically parsed into the bag.
    - A `dict` value (the final complete ToolCall exposed via AIMessageChunk
      `.tool_calls`) is authoritative: it replaces both the bag value and the raw
      buffer immediately.
    - `None` / non-dict-non-str scalars are ignored.
    """
    if tool_id is None:
        return
    session_raw: dict[str, list[str]] = _pending_raw.setdefault(session_id, {})
    buf: list[str] = session_raw.setdefault(tool_id, [])

    if isinstance(raw_args, dict):
        if raw_args:
            _pending_args.setdefault(session_id, {})[tool_id] = raw_args
            session_raw[tool_id] = []
        return

    if isinstance(raw_args, str):
        buf.append(raw_args)
        joined = "".join(buf)
        try:
            parsed = json.loads(joined)
        except Exception:
            return
        if isinstance(parsed, dict) and parsed:
            _pending_args.setdefault(session_id, {})[tool_id] = parsed
            session_raw[tool_id] = []
        return

    return


def _get_pending_args(session_id: str, tool_id: str | None) -> dict:
    """Read a tool's pending args for one session ({} when absent).

    Mirrors the old ``_pending_args.get(tool_id or "", {})`` wire contract so
    tool_start frames are unchanged.
    """
    return _pending_args.get(session_id, {}).get(tool_id or "", {})


def _pop_pending_args(session_id: str, tool_id: str) -> dict:
    """Consume a tool's pending args for one session ({} when absent).

    Mirrors the old ``_pending_args.pop(tool_id, {})`` contract for the
    tool_result frames (updates mode / HITL denial path).
    """
    return _pending_args.get(session_id, {}).pop(tool_id, {})


def _clear_pending_args(session_id: str) -> None:
    """Turn-end cleanup: drop ONLY this session's pending args.

    Replaces the old process-global ``_pending_args.clear()`` which wiped
    every session's state whenever any turn finished.
    """
    _pending_args.pop(session_id, None)


def _reasoning_delta(msg_chunk: BaseMessage) -> str:
    """Return the streamed reasoning delta carried on a message chunk.

    The reasoning normalizer (``models/LLMs/reasoning_normalizer.py``)
    guarantees every streamed chunk carries only its per-chunk DELTA under
    ``additional_kwargs["reasoning_content"]`` (provider alias keys folded in).
    The client APPENDS every ``{"type": "reasoning"}`` chunk it receives, so
    the value must be forwarded verbatim — cumulative values would duplicate
    the thinking text on the client, and langchain's own chunk aggregation
    concatenates string additional_kwargs values, so deltas also reconstruct
    the complete chain-of-thought on the final aggregated message (what gets
    checkpointed and persisted to the messages store).
    """
    kws: dict[str, Any] = getattr(msg_chunk, "additional_kwargs", None) or {}
    return kws.get("reasoning_content", "") or ""


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


async def _get_agent_history_list(session_id: str) -> list[BaseMessage]:
    agent = await built_agent()

    state = await agent.aget_state(config=build_agent_config(session_id))
    return state.values.get("messages", [])


def _get_content_list(multi_modal_message: MultiModalMessage) -> list[dict[str, str]]:
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
            if image_base64.startswith("data:image/"):
                # Already has the prefix, use as-is
                image_url = image_base64
            else:
                # No prefix, add one
                image_url = f"data:image/png;base64,{image_base64}"

            content_list.append({"type": "image_url", "image_url": {"url": image_url}})

    if multi_modal_message.image_bytes_list:
        for image_bytes in multi_modal_message.image_bytes_list:
            base64_str = base64.b64encode(image_bytes).decode("utf-8")
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


async def _get_generator(
    session_id: str,
    multi_modal_message: MultiModalMessage,
    is_stream: bool = True,
    origin: dict | None = None,
):
    start_time = time.time()

    logger.debug(f"Building agent: session_id={session_id}")

    # Rebuild the agent every turn with a FRESH main_llm -> httpx transport
    # pool. Reusing a long-lived pooled connection across WS turns goes stale
    # (DeepSeek's edge reaps an idle keep-alive connection ~15-17s) and the next
    # streaming POST dies mid-request as openai.APITimeoutError. Provably: the
    # same large payload streams in 8.0s on a fresh client but dies at ~16.78s on
    # the cached pool. The SQLite checkpointer persists session state
    # independently of the graph object, so this rebuild is safe.
    agent = await built_agent(force_rebuild=True)

    # Prepare the content_list
    content_list: list[dict[str, str]] = _get_content_list(multi_modal_message)

    elapsed = time.time() - start_time
    logger.debug(
        f"Agent generator prepared: session_id={session_id}, duration={elapsed:.2f}s, "
        f"is_stream={is_stream}, has_images={len(multi_modal_message.image_base64_list) if multi_modal_message.image_base64_list else 0}"
    )

    # origin (Task 4, subagent-origin-tagging): the subagent-completion carrier
    # tag {internal, provenance, run_id, status} forwarded verbatim from
    # auto_turn. None (real-user WS/channel paths) is legal — LangChain
    # metadata is Optional — and leaves the message untagged.
    input_dict = {
        "session_id": session_id,
        "messages": [HumanMessage(content=content_list, metadata=origin)],
    }
    if is_stream:
        return agent.astream(
            input=input_dict,
            config=build_agent_config(session_id),
            stream_mode=["messages", "updates"],
        )
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


async def async_generate(
    session_id: str,
    multi_modal_message: MultiModalMessage,
    is_stream: bool = True,
    origin: dict | None = None,
) -> AsyncGenerator[dict[str, str], None]:
    start_time = time.time()
    logger.debug(
        f"Agent execution started: session_id={session_id}, is_stream={is_stream}, "
        f"input_text_length={len(multi_modal_message.text) if multi_modal_message.text else 0}"
    )

    # Create the agent with assembled context
    ai_text: str = ""

    # reset curator reset_idle_for_seconds
    reset_idle_for_seconds()

    # Control answering
    state_register_mem.set_state(session_id, "answering", True)

    generator = None

    # Rolling model metadata accumulators, updated as chunks stream by. The
    # first chunk carries the model name; only the final chunk carries usage.
    meta_model_name: str | None = None
    meta_input_tokens: int | None = None
    meta_output_tokens: int | None = None

    try:
        if is_stream:
            # Stream directly from the context-assembled agent
            generator = await _get_generator(session_id, multi_modal_message, origin=origin)
            async for chunk in generator:
                # With stream_mode=["messages", "updates"], each chunk is (mode, data).
                # Only process the "messages" mode; skip "updates" mode chunks.
                if state_register_mem.get_state(session_id, "answering") is False:
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
                            args = _pop_pending_args(session_id, tool_id)
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
                if (
                    metadata.get("langgraph_node", None) != "model"
                    or metadata.get("lc_source") == "summarization"
                ):
                    continue

                if isinstance(msg_chunk, AIMessageChunk):
                    # Capture model + token usage metadata. The first chunk
                    # carries the model name; only the final chunk carries
                    # usage. Every lookup is guarded so a missing field NEVER
                    # breaks the stream.
                    try:
                        _resp_meta = getattr(msg_chunk, "response_metadata", None) or {}
                        _model_name = _resp_meta.get("model_name") or _resp_meta.get("model")
                        if _model_name:
                            meta_model_name = _model_name
                        _usage = getattr(msg_chunk, "usage_metadata", None)
                        if _usage:
                            if _usage.get("input_tokens") is not None:
                                meta_input_tokens = int(_usage["input_tokens"])
                            if _usage.get("output_tokens") is not None:
                                meta_output_tokens = int(_usage["output_tokens"])
                    except (KeyError, TypeError, AttributeError):
                        pass

                    # Tool call output logic
                    tool_calls: list[ToolCall] | list[ToolCallChunk] = (
                        msg_chunk.tool_calls
                        if msg_chunk.tool_calls and len(msg_chunk.tool_calls) > 0
                        else msg_chunk.tool_call_chunks
                    )
                    if (
                        len(tool_calls) > 0
                        or state_register_mem.get_state(session_id, "current_tool_id", "").strip()
                    ):
                        repeat_flag: bool = True  # Prevent duplicate tool call output
                        tool_id: str | None = (
                            None  # current tool call id (unknown for dict-typed access)
                        )
                        if len(tool_calls) > 0:
                            tool_call = tool_calls[0]

                            if tool_call["name"]:
                                if tool_call["name"].strip() or tool_call[
                                    "name"
                                ].strip() != state_register_mem.get_state(
                                    session_id, "current_tool_name"
                                ):
                                    state_register_mem.set_state(
                                        session_id, "current_tool_name", tool_call["name"]
                                    )

                            if tool_call["id"]:
                                tool_id = tool_call["id"]
                                if (
                                    tool_id.strip()
                                    or tool_id.strip()
                                    != state_register_mem.get_state(session_id, "current_tool_id")
                                ):
                                    state_register_mem.set_state(
                                        session_id, "current_tool_id", tool_id
                                    )
                                    repeat_flag = False

                        # Continuously refresh the pending arg-bag from the most
                        # complete ToolCall chunk. Tool calling is streamed as a
                        # sequence of fragments: the first chunk carries empty
                        # args, and later chunks progressively accumulate the full
                        # JSON. Capturing args only on the first fragment leaves
                        # _pending_args permanently empty, so tool_start/tool_result
                        # both carry {} on the wire (the tool bubble shows no args
                        # until the page is rebuilt from the checkpointed final
                        # ToolCall after refresh).
                        #
                        # This refresh runs on EVERY chunk (not just repeat_flag),
                        # so the accumulated dict from the final fragment supersedes
                        # the initial empty capture. A complete args dict always
                        # supersedes an earlier partial JSON string; we never let a
                        # later string fragment clobber an already-complete dict.
                        # Streamed tool-call args are fragmented: the first chunk
                        # carries the id with empty args, later (id-less) chunks
                        # carry partial-JSON string fragments. Accumulate them onto
                        # the effective tool id (persisted in state register) so the
                        # bag is populated by the time tool_start/tool_result emit.
                        eff_tool_id: str | None = (
                            tool_id
                            or state_register_mem.get_state(
                                session_id, "current_tool_id", ""
                            ).strip()
                            or None
                        )
                        # IMPORTANT: the args fragment MUST come from tool_call_chunks
                        # (the complete ordered partial-JSON stream, including the
                        # leading `{`), NOT from the `tool_calls` ToolCall dict whose
                        # args is an empty `{}` on the first chunk. Reading the dict
                        # loses the opening brace, so the accumulated string can never
                        # form valid JSON and tool_start/tool_result stay args={}.
                        _arg_frag: str | None = None
                        if msg_chunk.tool_call_chunks and len(msg_chunk.tool_call_chunks) > 0:
                            _arg_frag = msg_chunk.tool_call_chunks[0].get("args")
                        _accumulate_pending_args(session_id, eff_tool_id, _arg_frag)

                        if not repeat_flag:
                            tool_name = state_register_mem.get_state(
                                session_id, "current_tool_name", ""
                            )
                            ai_text += f"\n\n**Calling tool {tool_name}...**"
                            yield {
                                "type": "tool_start",
                                "content": tool_name,
                                "args": _get_pending_args(session_id, tool_id),
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

                    # Model reasoning output logic
                    # Reasoning models (DeepSeek thinking, GLM thinking, R1...)
                    # stream their chain-of-thought via
                    # `additional_kwargs['reasoning_content']` (NOT inline content).
                    # The normalizer guarantees per-chunk DELTAS on that key, so
                    # the value is forwarded verbatim — the client appends each
                    # "reasoning" chunk, and chunk aggregation reconstructs the
                    # complete CoT on the final message. Surfaces as a dedicated
                    # "reasoning" chunk so the client can render a collapsible
                    # thinking block on the same message as the final answer.
                    _reasoning = _reasoning_delta(msg_chunk)
                    if _reasoning and len(_reasoning) > 0:
                        yield {"type": "reasoning", "content": _reasoning}
                    # End model reasoning output logic
                    # End conversation output logic

        else:
            generator = await _get_generator(
                session_id, multi_modal_message, is_stream=False, origin=origin
            )
            result: dict[str, Any] = await generator
            res: str = result["messages"][-1].content
            ai_text += res
            yield {"type": "text", "content": res}
            # Non-stream: read model + token metadata from the final message.
            try:
                last_msg = result["messages"][-1]
                _resp_meta = getattr(last_msg, "response_metadata", None) or {}
                _model_name = _resp_meta.get("model_name") or _resp_meta.get("model")
                if _model_name:
                    meta_model_name = _model_name
                _usage = getattr(last_msg, "usage_metadata", None)
                if _usage:
                    if _usage.get("input_tokens") is not None:
                        meta_input_tokens = int(_usage["input_tokens"])
                    if _usage.get("output_tokens") is not None:
                        meta_output_tokens = int(_usage["output_tokens"])
            except (KeyError, TypeError, AttributeError):
                pass

        # Normal completion: surface the rolling model metadata as a final
        # "meta" chunk. Only yielded here — never on the exception paths below.
        yield {
            "type": "meta",
            "content": "",
            "model_name": meta_model_name or "",
            "input_tokens": meta_input_tokens or 0,
            "output_tokens": meta_output_tokens or 0,
        }

        elapsed = time.time() - start_time
        logger.debug(
            f"Agent execution completed: session_id={session_id}, duration={elapsed:.2f}s, "
            f"output_length={len(ai_text)}"
        )
    except asyncio.CancelledError:
        elapsed = time.time() - start_time
        yield {"type": "text", "content": "Request cancelled"}
        logger.debug(f"Agent execution cancelled: session_id={session_id}, duration={elapsed:.2f}s")
    except HeartbeatTimeoutError as e:
        elapsed = time.time() - start_time
        yield {
            "type": "text",
            "content": "\n\n**[Heartbeat Timeout]** Agent idle timeout exceeded — automatically terminated.",
        }
        logger.warning(
            f"Agent heartbeat timeout: session_id={session_id}, duration={elapsed:.2f}s, error={e}"
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
        # There is no pool to "release" here: the stale keep-alive connection
        # (which dies mid-request as openai.APITimeoutError) is handled by
        # rebuilding the graph with a FRESH main_llm -> httpx client at the START
        # of each turn (see the built_agent(force_rebuild=True) call above).
        # Closing the embedded AsyncOpenAI here would permanently kill it
        # ("Cannot send a request, as the client has been closed"), so we never
        # close it mid-lifecycle.
        # Reset tool tracking state
        state_register_mem.set_state(session_id, "current_tool_name", "")
        state_register_mem.set_state(session_id, "current_tool_id", "")
        state_register_mem.set_state(session_id, "answering", False)
        _clear_pending_args(session_id)


"""HITL interrupt detection — checks agent state for pending interrupts.

When the humanInTheLoop middleware calls ``interrupt()``, the agent stream
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
                    action_requests = (
                        value.get("action_requests", []) if isinstance(value, dict) else []
                    )
                    review_configs = (
                        value.get("review_configs", []) if isinstance(value, dict) else []
                    )
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
    logger.info(f"Agent resume started: session_id={session_id}, decision={decision}")

    agent = await built_agent(force_rebuild=True)
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

    # Accumulator for the stream-level (Layer C) repetition interception below.
    # Mirrors async_generate so the HITL resume path also cuts a repetitive tail
    # mid-stream instead of pushing it to the client.
    ai_text_stream: str = ""

    try:
        async for chunk in agent.astream(
            Command(resume=resume_value),
            config=config,
            stream_mode=["messages", "updates"],
        ):
            if state_register_mem.get_state(session_id, "answering") is False:
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
                        args = _pop_pending_args(session_id, tool_id)
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

            # HITL denial path: a rejected tool call never reaches the "tools"
            # node — the HumanInTheLoop middleware's after_model hook replaces it
            # with an artificial error ToolMessage inside its own middleware node
            # ("HumanInTheLoop.after_model"), so the updates-mode "tools" branch
            # above never sees it and the client got zero feedback for the
            # rejection. Emit the tool_result frame here, scoped to middleware
            # after_model nodes; normal tools-node results keep their existing
            # updates-mode path (the node filter below still skips them in
            # messages mode).
            _chunk_node = metadata.get("langgraph_node", None)
            if (
                isinstance(msg_chunk, ToolMessage)
                and isinstance(_chunk_node, str)
                and _chunk_node.endswith(".after_model")
            ):
                _hitl_args = _pop_pending_args(session_id, msg_chunk.tool_call_id)
                yield {
                    "type": "tool_result",
                    "content": _normalize_text(msg_chunk.content),
                    "tool_id": msg_chunk.tool_call_id,
                    "tool_name": getattr(msg_chunk, "name", "")
                    or state_register_mem.get_state(session_id, "current_tool_name", ""),
                    # _pending_args was cleared by the generate turn's finally
                    # block before the resume started; sending {} would wipe the
                    # args already shown on the client card, so emit null instead
                    # (client keeps its existing args when meta.args is falsy).
                    "args": _hitl_args or None,
                    "error": bool(getattr(msg_chunk, "status", None) == "error"),
                }
                continue

            if (
                metadata.get("langgraph_node", None) != "model"
                or metadata.get("lc_source") == "summarization"
            ):
                continue

            if isinstance(msg_chunk, AIMessageChunk):
                tool_calls = (
                    msg_chunk.tool_calls
                    if msg_chunk.tool_calls and len(msg_chunk.tool_calls) > 0
                    else msg_chunk.tool_call_chunks
                )
                if (
                    len(tool_calls) > 0
                    or state_register_mem.get_state(session_id, "current_tool_id", "").strip()
                ):
                    repeat_flag = True
                    tool_id: str | None = None  # current tool call id
                    if len(tool_calls) > 0:
                        tool_call = tool_calls[0]
                        if tool_call["name"]:
                            if tool_call["name"].strip() or tool_call[
                                "name"
                            ].strip() != state_register_mem.get_state(
                                session_id, "current_tool_name"
                            ):
                                state_register_mem.set_state(
                                    session_id, "current_tool_name", tool_call["name"]
                                )
                        if tool_call["id"]:
                            tool_id = tool_call["id"]
                            if tool_id.strip() or tool_id.strip() != state_register_mem.get_state(
                                session_id, "current_tool_id"
                            ):
                                state_register_mem.set_state(session_id, "current_tool_id", tool_id)
                                repeat_flag = False
                        # Same streaming-args fix as async_generate: read the partial
                        # JSON args fragment from tool_call_chunks (which includes the
                        # leading `{`) rather than the `tool_calls` dict (empty `{}` on
                        # the first chunk) so the accumulated buffer forms valid JSON.
                        eff_tool_id: str | None = (
                            tool_id
                            or state_register_mem.get_state(
                                session_id, "current_tool_id", ""
                            ).strip()
                            or None
                        )
                        _arg_frag = None
                        if msg_chunk.tool_call_chunks and len(msg_chunk.tool_call_chunks) > 0:
                            _arg_frag = msg_chunk.tool_call_chunks[0].get("args")
                        _accumulate_pending_args(session_id, eff_tool_id, _arg_frag)

                        if not repeat_flag:
                            tool_name = state_register_mem.get_state(
                                session_id, "current_tool_name", ""
                            )
                            yield {
                                "type": "tool_start",
                                "content": tool_name,
                                "args": _get_pending_args(session_id, tool_id),
                            }

                    # NOTE: tool_end handled in the updates-mode "tools" branch above
                    # (mirrors async_generate). Removed the old messages-mode gating
                    # on msg_chunk.content so text-free vision/image tools complete.

                if len(msg_chunk.content) > 0:
                    res: str = msg_chunk.content
                    ai_text_stream += res
                    yield {"type": "text", "content": res}

                # Surface thinking-mode chain-of-thought (DeepSeek / GLM / R1...).
                # The normalizer guarantees per-chunk DELTAS on
                # additional_kwargs["reasoning_content"] — forward verbatim,
                # mirroring async_generate so a HITL resume shows reasoning too.
                _reasoning = _reasoning_delta(msg_chunk)
                if _reasoning:
                    yield {"type": "reasoning", "content": _reasoning}

        elapsed = time.time() - start_time
        logger.debug(f"Agent resume completed: session_id={session_id}, duration={elapsed:.2f}s")
    except asyncio.CancelledError:
        yield {"type": "text", "content": "Request cancelled"}
        logger.debug(f"Agent resume cancelled: session_id={session_id}")
    except HeartbeatTimeoutError as e:
        yield {
            "type": "text",
            "content": "\n\n**[Heartbeat Timeout]** Agent idle timeout exceeded — automatically terminated.",
        }
        logger.warning(f"Agent resume heartbeat timeout: session_id={session_id}, error={e}")
    except Exception as e:
        logger.error(f"Agent resume failed: session_id={session_id}, error={str(e)}")
        logger.exception(e)
        raise e
    finally:
        # No pool-close here: closing the embedded AsyncOpenAI permanently kills
        # it ("Cannot send a request, as the client has been closed"). The stale
        # keep-alive connection problem is prevented by rebuilding the graph with
        # a fresh main_llm -> httpx client at the start of each turn via
        # built_agent(force_rebuild=True).
        state_register_mem.set_state(session_id, "current_tool_name", "")
        state_register_mem.set_state(session_id, "current_tool_id", "")
        state_register_mem.set_state(session_id, "answering", False)
        _clear_pending_args(session_id)


"""End HITL resume"""

"""End response generation logic"""

"""History retrieval logic"""


def get_history_by_turn_page(
    session_id: str, min_turn_num: int, turn_page_size: int, turn_page_num: int
) -> list[dict[str, Any]]:
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
    await clear_session_DAO(session_id=session_id)
    logger.debug(f"Session history cleared: session_id={session_id}")


"""End clear session history logic"""
