"""Shared stream-dispatch engine for ``async_generate`` / ``resume_agent`` (audit 2.1.1).

``server/service/messages.py`` carried two nearly identical copies of the
``stream_mode=["messages", "updates"]`` dispatch loop — updates-mode tool
handling, messages-mode tool-call tracking + streamed-args accumulation, and
text/reasoning emission — plus the tool-args stash and the per-chunk helpers
they rely on. This module owns the shared loop as a Template Method
(:class:`StreamTurn`); the two turn flavors live in ``messages.py`` as
subclasses overriding only their genuinely different hooks:

- ``_create_source``    — generate: context-assembled astream / ainvoke;
                          resume: ``Command(resume=...)`` astream;
- ``_extra_messages_frames`` — resume's HITL-denial tool_result path
                          (middleware ``*.after_model`` ToolMessages); the
                          generate turn has no counterpart;
- ``_note_tool_start``  — generate appends the ``**Calling tool ...**`` note
                          to the transcript accumulator; resume does not;
- ``_invoke_frames``    — generate's non-stream (``ainvoke``) emission;
- ``_final_frames``     — generate's terminal ``meta`` chunk (resume yields
                          none);
- ``_on_cancelled`` / ``_on_heartbeat_timeout`` — generate's best-effort
                          interrupted-marker writes; resume has none;
- ``_cleanup``          — both reset the shared tool-tracking state; generate
                          additionally closes the stream generator.

The pending tool-args stash (``_pending_args`` / ``_pending_raw``) and the
``_reasoning_delta`` / ``_normalize_text`` helpers moved here from
``messages.py`` verbatim (which re-imports them so their import path and the
per-session module state stay stable for existing tests).
"""

import asyncio
import json
import time
from typing import Any, AsyncGenerator, Literal

from loguru import logger
from langchain.messages import AIMessageChunk
from langchain_core.messages import BaseMessage, ToolCall, ToolCallChunk, ToolMessage
from runtime import state_register_mem
from agent.middlewares.heartbeat_staleness import HeartbeatTimeoutError


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


class StreamTurn:
    """Template Method over the ``["messages", "updates"]`` astream dispatch loop.

    Subclass contract (all hooks default to the resume turn's no-op behavior
    unless noted): see the module docstring. ``run`` yields the same typed
    dict chunks the two original generators yielded.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        # Transcript accumulator (drives the cancelled/timeout interrupt
        # marker on the generate turn; a running tool-call note is appended).
        self.ai_text: str = ""
        # Rolling model metadata accumulators, updated as chunks stream by. The
        # first chunk carries the model name; only the final chunk carries usage.
        self.meta_model_name: str | None = None
        self.meta_input_tokens: int | None = None
        self.meta_output_tokens: int | None = None

    # ---- hooks ----------------------------------------------------------

    async def _prepare(self) -> None:
        """Run before ``answering`` is set (before the try block)."""

    async def _create_source(self) -> tuple[Literal["stream", "invoke"], Any]:
        """Return the turn's source: a chunk stream or an awaitable result."""
        raise NotImplementedError

    def _note_tool_start(self, tool_name: str) -> None:
        """Side effect when a new tool_start fires (transcript note)."""

    def _extra_messages_frames(self, msg_chunk: BaseMessage, metadata: dict[str, Any]) -> list[dict]:
        """Frames to yield for messages-mode chunks consumed before the model
        filter (resume's HITL-denial tool_result path); empty = not consumed."""
        return []

    def _invoke_frames(self, result: dict[str, Any]) -> list[dict]:
        """Frames for a non-stream (ainvoke) result; updates meta accumulators."""
        return []

    def _final_frames(self) -> list[dict]:
        """Frames yielded after normal completion (generate: the meta chunk)."""
        return []

    async def _on_cancelled(self) -> None:
        """Best-effort persistence on the cancel path (generate: marker)."""

    async def _on_heartbeat_timeout(self, exc: HeartbeatTimeoutError) -> None:
        """Best-effort persistence on the timeout path (generate: marker)."""

    def _log_started(self) -> None: ...
    def _log_completed(self, elapsed: float) -> None: ...
    def _log_cancelled(self, elapsed: float) -> None: ...
    def _log_timeout(self, elapsed: float, exc: HeartbeatTimeoutError) -> None: ...
    def _log_failed(self, elapsed: float, exc: Exception) -> None: ...

    async def _cleanup(self, kind: Literal["stream", "invoke"] | None, source: Any) -> None:
        # Reset tool tracking state
        state_register_mem.set_state(self.session_id, "current_tool_name", "")
        state_register_mem.set_state(self.session_id, "current_tool_id", "")
        state_register_mem.set_state(self.session_id, "answering", False)
        _clear_pending_args(self.session_id)

    # ---- template -------------------------------------------------------

    async def run(self) -> AsyncGenerator[dict[str, str], None]:
        start_time = time.time()
        self._log_started()
        await self._prepare()

        # Control answering
        state_register_mem.set_state(self.session_id, "answering", True)

        kind: Literal["stream", "invoke"] | None = None
        source: Any = None
        try:
            kind, source = await self._create_source()

            if kind == "invoke":
                result: dict[str, Any] = await source
                for frame in self._invoke_frames(result):
                    yield frame
            else:
                async for chunk in source:
                    if state_register_mem.get_state(self.session_id, "answering") is False:
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
                                name = state_register_mem.get_state(
                                    self.session_id, "current_tool_name", ""
                                )
                                args = _pop_pending_args(self.session_id, tool_id)
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
                                state_register_mem.set_state(
                                    self.session_id, "current_tool_id", ""
                                )
                        continue
                    if mode != "messages":
                        continue

                    # For "messages" mode, data is (message_chunk, metadata_dict).
                    msg_chunk: BaseMessage = data[0]
                    metadata: dict[str, Any] = data[1]

                    extra_frames = self._extra_messages_frames(msg_chunk, metadata)
                    if extra_frames:
                        for frame in extra_frames:
                            yield frame
                        continue

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
                                self.meta_model_name = _model_name
                            _usage = getattr(msg_chunk, "usage_metadata", None)
                            if _usage:
                                if _usage.get("input_tokens") is not None:
                                    self.meta_input_tokens = int(_usage["input_tokens"])
                                if _usage.get("output_tokens") is not None:
                                    self.meta_output_tokens = int(_usage["output_tokens"])
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
                            or state_register_mem.get_state(
                                self.session_id, "current_tool_id", ""
                            ).strip()
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
                                        self.session_id, "current_tool_name"
                                    ):
                                        state_register_mem.set_state(
                                            self.session_id, "current_tool_name", tool_call["name"]
                                        )

                                if tool_call["id"]:
                                    tool_id = tool_call["id"]
                                    if (
                                        tool_id.strip()
                                        or tool_id.strip()
                                        != state_register_mem.get_state(
                                            self.session_id, "current_tool_id"
                                        )
                                    ):
                                        state_register_mem.set_state(
                                            self.session_id, "current_tool_id", tool_id
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
                                    self.session_id, "current_tool_id", ""
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
                            _accumulate_pending_args(self.session_id, eff_tool_id, _arg_frag)

                            if not repeat_flag:
                                tool_name = state_register_mem.get_state(
                                    self.session_id, "current_tool_name", ""
                                )
                                self._note_tool_start(tool_name)
                                yield {
                                    "type": "tool_start",
                                    "content": tool_name,
                                    "args": _get_pending_args(self.session_id, tool_id),
                                }

                        # NOTE: tool_end is emitted from the updates-mode "tools"
                        # branch (on the real ToolMessage), so a tool that produces no
                        # adjacent text still gets a completion signal. The old
                        # messages-mode implementation gated on msg_chunk.content, which
                        # is why image/vision tools (usually text-free) hung forever.
                        # End tool call output logic

                        # Conversation output logic
                        if len(msg_chunk.content) > 0:
                            res: str = msg_chunk.content
                            self.ai_text += res
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

            for frame in self._final_frames():
                yield frame

            elapsed = time.time() - start_time
            self._log_completed(elapsed)
        except asyncio.CancelledError:
            elapsed = time.time() - start_time
            # Task 6: persist the interrupted marker (best-effort) BEFORE the
            # cancel frame — reconcile the checkpointer transcript while ai_text
            # still holds the partial answer. Never masks the frame below.
            await self._on_cancelled()
            yield {"type": "text", "content": "Request cancelled"}
            self._log_cancelled(elapsed)
        except HeartbeatTimeoutError as e:
            elapsed = time.time() - start_time
            # Task 6: persist the interrupted marker (best-effort) BEFORE the
            # timeout frame — same contract as the cancel path above.
            await self._on_heartbeat_timeout(e)
            yield {
                "type": "text",
                "content": "\n\n**[Heartbeat Timeout]** Agent idle timeout exceeded — automatically terminated.",
            }
            self._log_timeout(elapsed, e)
        except Exception as e:
            elapsed = time.time() - start_time
            self._log_failed(elapsed, e)
            raise e
        finally:
            await self._cleanup(kind, source)
