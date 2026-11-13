"""Session-end memory salvage (job 2): snapshot a deleted session transcript and fork a bounded memory-extraction wave."""

import asyncio
from collections.abc import Sequence
from typing import Any
from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool
from loguru import logger
from config.features import BACKGROUND_REVIEW_CONFIG
from runtime import state_register_mem
from .background_scaffold import _build_session_model, _session_tool_face
from .memory_review import (
    _MEMORY_REVIEW_PROMPT,
    _build_memory_review_agent,
    _invoke_memory_review,
    _memory_only_tool_face,
    run_memory_consolidation,
)


#: Session deletion mirrors the compression cut without compressing: wave 1
#: covers the prefix that would have been discarded, wave 2 the preserved tail.
_SESSION_END_REVIEW_FIRST_PROMPT = (
    "This session is being deleted. The messages above are the earlier portion "
    "that a compression would have discarded. Rescue any durable information — "
    "user preferences, corrections, project conventions, or system-level pitfalls — "
    "before the transcript disappears.\n\n" + _MEMORY_REVIEW_PROMPT
)


_SESSION_END_REVIEW_REST_PROMPT = (
    "This session is being deleted. The messages above are the remaining portion "
    "after that compression line; memory was just refreshed from the earlier "
    "portion. Rescue any durable information that is not already saved.\n\n" + _MEMORY_REVIEW_PROMPT
)


# Session-end salvage: deletion snapshots the transcript, schedules this task,
# and returns to the purge. The derived session keys carry the fork's engine
# state so a deleted session's state is never recreated.
_SESSION_END_REVIEW_SESSION_SUFFIX = "::session-end-salvage"


_SESSION_END_REVIEW_TASKS: set[asyncio.Task[None]] = set()


_SESSION_END_REVIEW_INFLIGHT: set[str] = set()


_SESSION_END_SNAPSHOT_TIMEOUT_S = 5.0


def _session_end_cutoff(messages: list[BaseMessage], session_id: str) -> int:
    """The read-only compression cut for the captured session transcript.

    This deliberately does NOT run any compression step: no non-LLM pruning, no
    media offload, no summary, no checkpoint write. It only reuses the existing
    preserve-budget cutoff so the two salvage waves cover exactly the prefix a
    compression would have discarded and the tail it would have kept.
    """
    from agent.middlewares.summarization.core import Summarization
    from models.LLMs.main_llm import max_tokens as main_llm_context_window

    try:
        window = int(main_llm_context_window) if main_llm_context_window else None
    except (TypeError, ValueError):
        window = None
    summarizer = Summarization(model=None, main_llm_context_window=window)
    cutoff = summarizer._determine_cutoff(messages, session_id)
    return max(0, min(int(cutoff), len(messages)))


async def _snapshot_session_for_salvage(
    session_id: str,
) -> tuple[list[BaseMessage], Any, str, list[BaseTool], int] | None:
    """Capture everything the detached review needs before the purge starts.

    The checkpoint read is the only framework interaction. The model, system
    prompt and tool face are captured here rather than resolved by the background
    task because the session's config/registers are gone once the purge lands.
    """
    from agent.middlewares.system_prompt.core import _get_and_reload_system_prompt
    from agent.state_port import read_messages

    messages = list(await read_messages(session_id))
    if not messages:
        return None
    system_prompt = _get_and_reload_system_prompt(session_id)
    model = _build_session_model(session_id)
    tools = list(_session_tool_face(session_id))
    if not any(getattr(tool, "name", "") == "memory" for tool in tools):
        tools = _memory_only_tool_face()
    cutoff = _session_end_cutoff(messages, session_id)
    return messages, model, system_prompt, tools, cutoff


def _rebuild_session_end_prompt(session_id: str, fallback: str) -> str:
    """Reload memory after wave 1 and rebuild the second wave's system prompt.

    The rebuild is intentionally NON-persisting: the session is being deleted,
    so writing the prompt back through ``_persist_system_prompt`` would recreate
    its state-register rows. A rebuild failure falls back to the captured prompt.
    """
    try:
        from agent.tools.memory import memory_store
        from workspace.prompt_builder import build_system_prompt

        memory_store.load_from_store()
        rebuilt = build_system_prompt(session_id=session_id)
        return rebuilt or fallback
    except Exception:  # noqa: BLE001 - the second wave must still run
        logger.warning(
            "session-end memory review: prompt rebuild failed; using captured prompt session={}",
            session_id,
            exc_info=True,
        )
        return fallback


async def _run_session_end_wave(
    *,
    session_id: str,
    wave: int,
    model: Any,
    system_prompt: str,
    tools: Sequence[BaseTool],
    messages: Sequence[BaseMessage],
    prompt_template: str,
) -> bool:
    """Run one detached review wave under its own derived session key."""
    derived_session_id = f"{session_id}{_SESSION_END_REVIEW_SESSION_SUFFIX}:{wave}"
    try:
        agent = _build_memory_review_agent(
            model,
            system_prompt,
            tools,
            allowed_metadata_key="background_review_memory",
        )
        timeout = max(1.0, float(BACKGROUND_REVIEW_CONFIG["session_end_review_timeout_s"]))
        await asyncio.wait_for(
            _invoke_memory_review(
                agent,
                derived_session_id,
                messages,
                prompt_template,
                origin_session=session_id,
            ),
            timeout=timeout,
        )
        return True
    except Exception:  # noqa: BLE001 - a deleted session's review is best-effort
        logger.exception(
            "session-end memory review wave {} failed (fail-open) session={}",
            wave,
            session_id,
        )
        return False
    finally:
        try:
            state_register_mem.clear_session(derived_session_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "session-end memory review: failed to clear derived state {}: {}",
                derived_session_id,
                exc,
            )


async def _run_session_end_salvage(
    *,
    session_id: str,
    model: Any,
    system_prompt: str,
    tools: Sequence[BaseTool],
    first_wave: Sequence[BaseMessage],
    rest_wave: Sequence[BaseMessage],
) -> None:
    """Run the two deletion-time waves in order, each independently fail-open."""
    first_ok = False
    updated_prompt = system_prompt
    if first_wave:
        first_ok = await _run_session_end_wave(
            session_id=session_id,
            wave=1,
            model=model,
            system_prompt=system_prompt,
            tools=tools,
            messages=first_wave,
            prompt_template=_SESSION_END_REVIEW_FIRST_PROMPT,
        )
    if rest_wave:
        if first_ok:
            updated_prompt = _rebuild_session_end_prompt(session_id, system_prompt)
        await _run_session_end_wave(
            session_id=session_id,
            wave=2,
            model=model,
            system_prompt=updated_prompt,
            tools=_memory_only_tool_face(),
            messages=rest_wave,
            prompt_template=_SESSION_END_REVIEW_REST_PROMPT,
        )
    try:
        await run_memory_consolidation(
            session_id,
            max_turns=5,
            timeout_s=60,
            origin_session=session_id,
            system_prompt=updated_prompt,
        )
    except Exception:  # noqa: BLE001 - deletion continues without consolidation
        logger.exception("session-end memory consolidation failed (fail-open) for {}", session_id)


async def salvage_session_memory(session_id: str) -> bool:
    """Snapshot a session and schedule its detached session-end memory review.

    Called by ``purge_session`` BEFORE any destructive step. It returns as soon
    as the snapshot is captured and the background task is scheduled; the purge
    therefore proceeds immediately. The snapshot has its own short budget so a
    slow checkpoint read cannot delay deletion indefinitely.

    Scope is the MAIN session only; side chats and subagent children are not
    passed through this function. The review writes global memory files, uses no
    checkpointer, and never resurrects the deleted session's state.
    """
    if not bool(BACKGROUND_REVIEW_CONFIG.get("session_end_review_enabled", True)):
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("session-end memory review: no running loop for {}", session_id)
        return False
    if session_id in _SESSION_END_REVIEW_INFLIGHT:
        return False

    _SESSION_END_REVIEW_INFLIGHT.add(session_id)
    try:
        snapshot = await asyncio.wait_for(
            _snapshot_session_for_salvage(session_id),
            timeout=_SESSION_END_SNAPSHOT_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        _SESSION_END_REVIEW_INFLIGHT.discard(session_id)
        raise
    except Exception:  # noqa: BLE001 - deletion continues without salvage
        _SESSION_END_REVIEW_INFLIGHT.discard(session_id)
        logger.warning(
            "session-end memory review: snapshot failed/ timed out (fail-open) session={}",
            session_id,
            exc_info=True,
        )
        return False
    if snapshot is None:
        _SESSION_END_REVIEW_INFLIGHT.discard(session_id)
        logger.debug("session-end memory review: no transcript to salvage for {}", session_id)
        return False

    messages, model, system_prompt, tools, cutoff = snapshot
    if cutoff > 0:
        first_wave = messages[:cutoff]
        rest_wave = messages[cutoff:]
    else:
        first_wave = messages
        rest_wave = []

    coro = _run_session_end_salvage(
        session_id=session_id,
        model=model,
        system_prompt=system_prompt,
        tools=tools,
        first_wave=first_wave,
        rest_wave=rest_wave,
    )
    try:
        task = loop.create_task(coro)
    except RuntimeError:  # pragma: no cover - the loop vanished after the check
        coro.close()
        _SESSION_END_REVIEW_INFLIGHT.discard(session_id)
        return False

    _SESSION_END_REVIEW_TASKS.add(task)

    def _done(completed: asyncio.Task[None]) -> None:
        _SESSION_END_REVIEW_TASKS.discard(completed)
        _SESSION_END_REVIEW_INFLIGHT.discard(session_id)

    task.add_done_callback(_done)
    logger.info(
        "session-end memory review scheduled session={} cutoff={} first={} rest={}",
        session_id,
        cutoff,
        len(first_wave),
        len(rest_wave),
    )
    return True
