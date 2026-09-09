import asyncio
import base64
from loguru import logger
from agent import built_agent
from langgraph.types import Command
from typing import Any, Literal
from collections.abc import AsyncGenerator
from runtime import state_register_mem
from context_engine import get_session_ids
from type.message import MultiModalMessage
from pub_func import build_agent_config, is_url
from ..DAO import clear_session as clear_session_dao
from context_engine.curator import reset_idle_for_seconds
from agent.middlewares.heartbeat_staleness import HeartbeatTimeoutError
from context_engine import get_history_by_turn_page as _get_history_by_turn_page
from langchain_core.messages import HumanMessage, BaseMessage, ToolMessage

from .stream_dispatch import (
    StreamTurn,
    _normalize_text,
    _pop_pending_args,
)


def _get_content_list(multi_modal_message: MultiModalMessage) -> list[str | dict[str, Any]]:
    user_text: str = multi_modal_message.text
    content_list: list[str | dict[str, Any]] = [{"type": "text", "text": user_text}]

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


"""Response generation logic — yields typed dict chunks

Each yielded item is a dict ``{"type": <str>, "content": <str>}`` where
``type`` is one of:
- ``"text"``       — conversational text fragment
- ``"tool_start"`` — a tool invocation begins (content = tool name)
- ``"tool_end"``   — a tool invocation completes (content = tool name)

Callers that only need the plain text (e.g. channel consumers) can
join ``chunk["content"]`` for every item.

Audit 2.1.1: ``async_generate`` and ``resume_agent`` share the
``stream_mode=["messages", "updates"]`` dispatch loop via the
:class:`~server.service.stream_dispatch.StreamTurn` Template Method in
``server/service/stream_dispatch.py`` (which also owns the pending tool-args
stash and the per-chunk helpers, re-imported here to keep their import
paths stable). The two turn flavors below override only their genuinely
different hooks.
"""


async def _write_interrupt_marker(
    session_id: str, partial_text: str, reason: Literal["cancelled", "heartbeat_timeout"]
) -> None:
    """Persist the interrupted-turn marker on ``async_generate``'s cancellation
    paths (plan / G3-allowed touchpoint: ONE await per handler, placed
    BEFORE the terminal frame so the partial transcript is reconciled while
    ``ai_text`` still holds it).

    Best-effort by contract: a failing marker write must never mask the
    "Request cancelled" frame nor break generator teardown, so every
    internal failure is logged and swallowed (``write_interrupted_marker``
    already guards its own body; this outer net also covers the import).
    """
    try:
        from .interrupt_marker import write_interrupted_marker

        await write_interrupted_marker(
            session_id, build_agent_config(session_id), partial_text, reason
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - best-effort hook on an exception path
        logger.warning(
            f"Interrupted-marker write failed (best-effort): session_id={session_id}, "
            f"reason={reason}, error={e}"
        )


# Phase 2 text continuation: injected verbatim (hermes-agent proven prompt)
# after a max_tokens truncation with no tool calls; the checkpointer reloads
# the truncated AIMessage so the model resumes mid-answer.
_CONTINUATION_PROMPT = (
    "[System: Your previous response was truncated by the output "
    "length limit. Continue exactly where you left off. Do not "
    "restart or repeat prior text. Finish the answer directly.]"
)
_MAX_CONTINUATION_RETRIES = 4


class _GenerateTurn(StreamTurn):
    """A normal user turn (stream or non-stream) through the context-assembled agent."""

    def __init__(
        self,
        session_id: str,
        multi_modal_message: MultiModalMessage,
        is_stream: bool = True,
        origin: dict | None = None,
    ) -> None:
        super().__init__(session_id)
        self.multi_modal_message = multi_modal_message
        self.is_stream = is_stream
        self.origin = origin
        # Agent reference held across the turn so a Phase 2 text continuation
        # can re-stream through the SAME checkpointer-backed graph (the new
        # HumanMessage appends to the truncated AIMessage in state).
        self._agent: Any = None
        self._continuation_retries: int = 0
        self._is_continuation: bool = False

    async def _prepare(self) -> None:
        # reset curator reset_idle_for_seconds
        reset_idle_for_seconds()
        self._agent = await built_agent(force_rebuild=True)

    def _log_started(self) -> None:
        logger.debug(
            f"Agent execution started: session_id={self.session_id}, is_stream={self.is_stream}, "
            f"input_text_length={len(self.multi_modal_message.text) if self.multi_modal_message.text else 0}"
        )

    async def _create_source(self) -> tuple[Literal["stream", "invoke"], Any]:
        if self._is_continuation:
            # Phase 2: the checkpointer reloads the truncated AIMessage, so a
            # bare continuation HumanMessage makes the model resume mid-answer.
            input_dict: dict[str, Any] = {
                "session_id": self.session_id,
                "messages": [HumanMessage(content=_CONTINUATION_PROMPT)],
            }
        else:
            content_list: list[str | dict[str, Any]] = _get_content_list(self.multi_modal_message)
            input_dict = {
                "session_id": self.session_id,
                "messages": [HumanMessage(content=content_list, metadata=self.origin)],
            }
        if self.is_stream:
            return "stream", self._agent.astream(
                input=input_dict,
                config=build_agent_config(self.session_id),
                stream_mode=["messages", "updates"],
            )
        return (
            "invoke",
            self._agent.ainvoke(input=input_dict, config=build_agent_config(self.session_id)),
        )

    def _note_tool_start(self, tool_name: str) -> None:
        self.ai_text += f"\n\n**Calling tool {tool_name}...**"

    def _invoke_frames(self, result: dict[str, Any]) -> list[dict]:
        res: str = result["messages"][-1].content
        self.ai_text += res
        # Non-stream: read model + token metadata from the final message.
        try:
            last_msg = result["messages"][-1]
            _resp_meta = getattr(last_msg, "response_metadata", None) or {}
            _model_name = _resp_meta.get("model_name") or _resp_meta.get("model")
            if _model_name:
                self.meta_model_name = _model_name
            _usage = getattr(last_msg, "usage_metadata", None)
            if _usage:
                if _usage.get("input_tokens") is not None:
                    self.meta_input_tokens = int(_usage["input_tokens"])
                if _usage.get("output_tokens") is not None:
                    self.meta_output_tokens = int(_usage["output_tokens"])
            _finish = _resp_meta.get("finish_reason") or _resp_meta.get("stop_reason")
            if _finish:
                self.meta_finish_reason = _finish
            self._has_tool_calls = bool(getattr(last_msg, "tool_calls", None))
        except (KeyError, TypeError, AttributeError):  # noqa: S110
            pass
        return [{"type": "text", "content": res}]

    def _final_frames(self) -> list[dict]:
        # Normal completion: surface the rolling model metadata as a final
        # "meta" chunk. Only yielded on the normal path — never on the
        # exception paths below.
        return [
            {
                "type": "meta",
                "content": "",
                "model_name": self.meta_model_name or "",
                "input_tokens": self.meta_input_tokens or 0,
                "output_tokens": self.meta_output_tokens or 0,
                "finish_reason": self.meta_finish_reason or "",
            }
        ]

    def _should_text_continue(self) -> bool:
        if self.meta_finish_reason == "content_filter":
            # Flag the middleware layer so the next model call can fall back
            # or terminate instead of re-prompting a filtered response.
            state_register_mem.set_state(self.session_id, "llm_content_filter_blocked", True)
            return False
        return (
            self.meta_finish_reason in ("length", "max_tokens")
            and not self._has_tool_calls
            and self._continuation_retries < _MAX_CONTINUATION_RETRIES
        )

    def _prepare_continuation(self) -> None:
        self._is_continuation = True
        self._continuation_retries += 1
        # The next model call's finish_reason overwrites these; clearing here
        # prevents a stale truncation value from re-triggering the loop if the
        # continuation stream carries no metadata.
        self.meta_finish_reason = None
        self._has_tool_calls = False

    async def _on_cancelled(self) -> None:
        await _write_interrupt_marker(self.session_id, self.ai_text, "cancelled")

    async def _on_heartbeat_timeout(self, exc: HeartbeatTimeoutError) -> None:
        await _write_interrupt_marker(self.session_id, self.ai_text, "heartbeat_timeout")

    def _log_completed(self, elapsed: float) -> None:
        logger.debug(
            f"Agent execution completed: session_id={self.session_id}, duration={elapsed:.2f}s, "
            f"output_length={len(self.ai_text)}"
        )

    def _log_cancelled(self, elapsed: float) -> None:
        logger.debug(
            f"Agent execution cancelled: session_id={self.session_id}, duration={elapsed:.2f}s"
        )

    def _log_timeout(self, elapsed: float, exc: HeartbeatTimeoutError) -> None:
        logger.warning(
            f"Agent heartbeat timeout: session_id={self.session_id}, duration={elapsed:.2f}s, error={exc}"
        )

    def _log_failed(self, elapsed: float, exc: Exception) -> None:
        logger.error(
            f"Agent execution failed: session_id={self.session_id}, duration={elapsed:.2f}s, "
            f"error={str(exc)}"
        )
        logger.exception(exc)

    async def _cleanup(self, kind: Literal["stream", "invoke"] | None, source: Any) -> None:
        # Gracefully close the async generator to avoid GeneratorExit/RuntimeError
        if kind == "stream" and source is not None:
            try:
                await source.aclose()
            except Exception:  # noqa: S110
                pass  # GeneratorExit is expected and harmless
        self._agent = None
        # There is no pool to "release" here: the stale keep-alive connection
        # (which dies mid-request as openai.APITimeoutError) is handled by
        # rebuilding the graph with a FRESH main_llm -> httpx client at the START
        # of each turn (see the built_agent(force_rebuild=True) call in
        # _GenerateTurn._prepare). Closing the embedded AsyncOpenAI here would
        # permanently kill it ("Cannot send a request, as the client has been
        # closed"), so we never close it mid-lifecycle.
        await super()._cleanup(kind, source)


class _ResumeTurn(StreamTurn):
    """A HITL resume turn: continues the agent after a human decision."""

    def __init__(
        self,
        session_id: str,
        decision: str,
        message: str = "",
        edited_args: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(session_id)
        self.decision = decision
        self.message = message
        self.edited_args = edited_args
        self._agent: Any = None
        self._config: Any = None
        self._resume_value: dict[str, Any] = {}

    def _log_started(self) -> None:
        logger.info(f"Agent resume started: session_id={self.session_id}, decision={self.decision}")

    async def _prepare(self) -> None:
        self._agent = await built_agent(force_rebuild=True)
        self._config = build_agent_config(self.session_id)

        # Inject session_id into the resume value. On a normal turn it arrives via the
        # graph input dict (see _GenerateTurn._create_source), but Command(resume=...) merges only the
        # resume value into state — without session_id here, MultimodalProcessor's
        # _before_agent_impl would raise "Not pass session_id" on resume.
        self._resume_value = {
            "session_id": self.session_id,
            "decisions": [{"type": self.decision, "message": self.message}],
        }
        if self.decision == "edit" and self.edited_args is not None:
            self._resume_value["decisions"][0]["edited_action"] = {"args": self.edited_args}

    async def _create_source(self) -> tuple[Literal["stream", "invoke"], Any]:
        return (
            "stream",
            self._agent.astream(
                Command(resume=self._resume_value),
                config=self._config,
                stream_mode=["messages", "updates"],
            ),
        )

    def _extra_messages_frames(
        self, msg_chunk: BaseMessage, metadata: dict[str, Any]
    ) -> list[dict]:
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
        if not (
            isinstance(msg_chunk, ToolMessage)
            and isinstance(_chunk_node, str)
            and _chunk_node.endswith(".after_model")
        ):
            return []
        _hitl_args = _pop_pending_args(self.session_id, msg_chunk.tool_call_id)
        return [
            {
                "type": "tool_result",
                "content": _normalize_text(msg_chunk.content),
                "tool_id": msg_chunk.tool_call_id,
                "tool_name": getattr(msg_chunk, "name", "")
                or state_register_mem.get_state(self.session_id, "current_tool_name", ""),
                # _pending_args was cleared by the generate turn's finally
                # block before the resume started; sending {} would wipe the
                # args already shown on the client card, so emit null instead
                # (client keeps its existing args when meta.args is falsy).
                "args": _hitl_args or None,
                "error": bool(getattr(msg_chunk, "status", None) == "error"),
            }
        ]

    def _log_completed(self, elapsed: float) -> None:
        logger.debug(
            f"Agent resume completed: session_id={self.session_id}, duration={elapsed:.2f}s"
        )

    def _log_cancelled(self, elapsed: float) -> None:
        logger.debug(f"Agent resume cancelled: session_id={self.session_id}")

    def _log_timeout(self, elapsed: float, exc: HeartbeatTimeoutError) -> None:
        logger.warning(f"Agent resume heartbeat timeout: session_id={self.session_id}, error={exc}")

    def _log_failed(self, elapsed: float, exc: Exception) -> None:
        logger.error(f"Agent resume failed: session_id={self.session_id}, error={str(exc)}")
        logger.exception(exc)

    async def _cleanup(self, kind: Literal["stream", "invoke"] | None, source: Any) -> None:
        # No pool-close here: closing the embedded AsyncOpenAI permanently kills
        # it ("Cannot send a request, as the client has been closed"). The stale
        # keep-alive connection problem is prevented by rebuilding the graph with
        # a fresh main_llm -> httpx client at the start of each turn via
        # built_agent(force_rebuild=True).
        await super()._cleanup(kind, source)


async def async_generate(
    session_id: str,
    multi_modal_message: MultiModalMessage,
    is_stream: bool = True,
    origin: dict | None = None,
) -> AsyncGenerator[dict[str, str]]:
    engine = _GenerateTurn(session_id, multi_modal_message, is_stream, origin)
    async for frame in engine.run():
        yield frame


async def resume_agent(
    session_id: str,
    decision: str,
    message: str = "",
    edited_args: dict[str, Any] | None = None,
) -> AsyncGenerator[dict[str, str]]:
    """Resume the agent after a HITL interrupt.

    Args:
        session_id:  Active session ID.
        decision:    ``"approve"``, ``"reject"``, or ``"edit"``.
        message:     Optional user message accompanying the decision.
        edited_args: When ``decision == "edit"``, the new tool arguments.

    Yields:
        Same chunk format as :func:`async_generate`.
    """
    engine = _ResumeTurn(session_id, decision, message, edited_args)
    async for frame in engine.run():
        yield frame


"""End HITL resume"""

"""End response generation logic"""

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
    await clear_session_dao(session_id=session_id)
    logger.debug(f"Session history cleared: session_id={session_id}")


"""End clear session history logic"""
