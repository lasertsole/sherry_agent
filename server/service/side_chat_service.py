"""Ephemeral side-chat sessions: create, inherit context, purge.

A side chat is a normal session with a parent link (``side_chat_store``), but
its lifecycle is bound to that parent and to the right-sidebar tab. It is a
COMPLETE fork of the parent agent: its first turn is seeded with the parent's
checkpoint transcript and the parent's per-session settings, while the copied
messages carry a model-only marker so the side chat's own visible history stays
empty. This module also owns the bounded text fallback for links created before
the checkpoint fork and the full purge used by both tab-close and parent
deletion.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from loguru import logger
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from pub.func.validator import is_safe_session_id
from pub.func.estimate_tokens import estimate_text_tokens
from runtime import StateKey
from server.DAO import side_chat_store

__all__ = [
    "build_parent_context",
    "create_side_chat",
    "delete_side_chat",
    "get_checkpoint_fork_for_turn",
    "get_context_for_turn",
    "purge_side_chat_children",
    "side_chat_ids",
]

#: Model-only context budget; the parent transcript is trimmed from the OLDEST
#: end until it fits. The selected quote is not part of this text (it travels
#: through the composer), so trimming old turns cannot remove the user's focus.
_CONTEXT_MAX_TOKENS = 12_000
_CONTEXT_MAX_TURNS = 40
_MAX_ROW_CHARS = 4_000

#: Rows whose origin is NOT the user's own words are system notices/steering;
#: they are meaningless in a side conversation and are skipped.
_INTERNAL_ORIGINS = {
    "git_head",
    "project_dir",
    "quality_gate",
    "subagent_completion",
    "task_intent",
    "todo_continuation",
}

_CONTEXT_BOUNDARY = (
    "<side_chat_context>\n"
    "The following is the parent session's history, inherited for reference only.\n"
    "Do not automatically continue the parent's active work; answer only new "
    "questions asked in this side chat. Modify the workspace only when the user "
    "explicitly asks you to do so here.\n\n"
)

_CHECKPOINT_BOUNDARY = (
    "<side_chat_context>\n"
    "The preceding conversation is the parent session's completed history, "
    "inherited as a complete fork for reference only. Do not automatically "
    "continue the parent's active work; answer only new questions asked in this "
    "side chat. Modify the workspace only when the user explicitly asks you to "
    "do so here.\n"
    "</side_chat_context>"
)

#: Per-session settings a side chat inherits so it behaves like the main agent
#: (same model, thinking, agent face, directory, access mode and budget).
_SIDE_CHAT_INHERITED_KEYS = (
    StateKey.LLM_MAIN_MODEL,
    StateKey.LLM_THINKING_ENABLED,
    StateKey.CUSTOM_THINKING_MAPPING,
    StateKey.SEARCH_BACKEND,
    StateKey.TOKEN_BUDGET_CAP,
    StateKey.AGENT_CONFIG,
    StateKey.PROJECT_DIR,
    StateKey.HITL_SESSION_YOLO,
    StateKey.HITL_SESSION_CONFIRM_ALL,
)


def _format_transcript(parent_session_id: str) -> str:
    """Build a bounded text transcript of the parent's persisted conversation."""
    from context_engine import get_messages_by_lastest_n_turns

    rows = get_messages_by_lastest_n_turns(
        parent_session_id, last_n=_CONTEXT_MAX_TURNS, only_eligible=True
    )

    # The store returns newest turn first (and rows inside a turn are not a
    # guaranteed transcript order); sort by turn then message id so the model
    # reads user → assistant → tool in their real order.
    def _order(row: dict[str, Any]) -> tuple[int, int]:
        try:
            return int(row.get("turn_num") or 0), int(row.get("id") or 0)
        except (TypeError, ValueError):
            return 0, 0

    lines: list[str] = []
    for row in sorted(rows, key=_order):
        role = str(row.get("role") or "")
        if role not in {"human", "ai", "tool"}:
            continue
        origin = str(row.get("origin") or "")
        if role == "human" and origin and origin != "user":
            continue
        if origin in _INTERNAL_ORIGINS:
            continue
        content = row.get("content")
        if isinstance(content, list):
            # Multimodal user messages arrive as content blocks; keep the text
            # blocks (media has no useful transcript form here).
            content = "\n".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        if not isinstance(content, str) or not content.strip():
            continue
        content = content[:_MAX_ROW_CHARS]
        if role == "human":
            label = "User"
        elif role == "ai":
            label = "Assistant"
        else:
            name = str(row.get("tool_name") or "").strip()
            label = f"Tool{(' ' + name) if name else ''}"
        lines.append(f"{label}: {content}")

    if not lines:
        return ""
    # Keep the newest part when the transcript exceeds the budget. A line is
    # the smallest useful unit; a single oversized line was already capped.
    while lines:
        text = "\n\n".join(lines)
        if estimate_text_tokens(text) <= _CONTEXT_MAX_TOKENS:
            break
        lines.pop(0)
    if not lines:
        return ""
    return _CONTEXT_BOUNDARY + "\n\n".join(lines) + "\n</side_chat_context>"


async def build_parent_context(parent_session_id: str) -> str:
    """Best-effort parent transcript; a failure means an empty context."""
    try:
        return await asyncio.to_thread(_format_transcript, parent_session_id)
    except Exception as exc:  # noqa: BLE001 - context inheritance is fail-open
        logger.warning("side chat: parent context build failed for {}: {}", parent_session_id, exc)
        return ""


def _copy_parent_session_settings(parent_session_id: str, child_session_id: str) -> int:
    """Copy the parent's per-session settings onto the child (fail-open)."""
    from server.service.session_settings_service import _read_value, _write_value

    copied = 0
    for key in _SIDE_CHAT_INHERITED_KEYS:
        try:
            value = _read_value(parent_session_id, key)
            if value is None:
                continue
            _write_value(child_session_id, key, value)
            copied += 1
        except Exception:
            logger.warning("side chat: failed to inherit {} from {}", key, parent_session_id)
    return copied


def _mark_inherited(message: BaseMessage) -> BaseMessage:
    """Mark one copied parent message as model-only side-chat context."""
    kwargs = dict(getattr(message, "additional_kwargs", None) or {})
    kwargs["side_chat_context"] = True
    return message.model_copy(update={"additional_kwargs": kwargs})


def _completed_history(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Cut a parent transcript at its last completed assistant answer.

    A parent caught mid-tool (a pending HITL approval or an interrupted run)
    leaves a trailing AIMessage whose tool calls have no results; copying that
    into the child would make its first turn execute the parent's unfinished
    call. The last call-less assistant message is the stable fork boundary.
    """
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, AIMessage) and not getattr(message, "tool_calls", None):
            return list(messages[: index + 1])
    return []


async def get_checkpoint_fork_for_turn(session_id: str) -> list[BaseMessage] | None:
    """The parent's checkpoint messages for a forked side chat's FIRST turn.

    This is the real fork: the child model sees every message the parent model
    saw (including browser/tool results that never reached the message store).
    The messages are marked ``side_chat_context`` so MessagePersistence keeps
    them out of the child's visible history.

    Falls back to the bounded text snapshot when the parent checkpoint cannot
    be read, and returns None after the child has its own messages.
    """
    row = side_chat_store.get_side_chat(session_id)
    if row is None or not int(row.get("fork_mode") or 0):
        return None
    try:
        from context_engine.store.core import session_has_messages

        if session_has_messages(session_id):
            return None
    except Exception:  # noqa: BLE001 - a probe failure must not inject twice
        return None

    parent_session_id = str(row.get("parent_session_id") or "")
    parent_messages: list[BaseMessage] = []
    if parent_session_id:
        try:
            from agent.state_port import read_messages

            parent_messages = _completed_history(list(await read_messages(parent_session_id)))
        except Exception as exc:  # noqa: BLE001 - fall back to the text snapshot
            logger.warning(
                "side chat: parent checkpoint read failed for {}: {}", parent_session_id, exc
            )
    if parent_messages:
        return [
            *(_mark_inherited(message) for message in parent_messages),
            HumanMessage(
                content=_CHECKPOINT_BOUNDARY,
                additional_kwargs={"side_chat_context": True},
                metadata={"internal": True, "provenance": "side_chat_context"},
            ),
        ]

    fallback = str(row.get("context_text") or "")
    if fallback:
        return [
            HumanMessage(
                content=fallback,
                additional_kwargs={"side_chat_context": True},
                metadata={"internal": True, "provenance": "side_chat_context"},
            )
        ]
    return None


async def create_side_chat(parent_session_id: str) -> dict[str, Any]:
    """Create a child id + parent link + checkpoint-fork inheritance."""
    if not is_safe_session_id(parent_session_id):
        raise ValueError("invalid parent_session_id")
    if side_chat_store.get_side_chat(parent_session_id) is not None:
        raise ValueError("a side chat cannot parent another side chat")

    child_session_id = uuid.uuid4().hex
    context_text = await build_parent_context(parent_session_id)
    side_chat_store.create_side_chat(
        child_session_id, parent_session_id, context_text, fork_mode=True
    )
    copied = _copy_parent_session_settings(parent_session_id, child_session_id)
    logger.info(
        "side chat created: child={} parent={} context_chars={} settings={}",
        child_session_id,
        parent_session_id,
        len(context_text),
        copied,
    )
    return {
        "session_id": child_session_id,
        "parent_session_id": parent_session_id,
        "inherited": True,
    }


def get_context_for_turn(session_id: str) -> str | None:
    """The inherited transcript for a child's FIRST turn, or None afterwards."""
    row = side_chat_store.get_side_chat(session_id)
    if row is None:
        return None
    if int(row.get("fork_mode") or 0):
        # New side chats use the checkpoint fork; this text path is only for
        # links created by the first implementation.
        return None
    context_text = str(row.get("context_text") or "")
    if not context_text:
        return None
    try:
        from context_engine.store.core import session_has_messages

        if session_has_messages(session_id):
            return None
    except Exception:  # noqa: BLE001 - a probe failure must not inject twice
        return None
    return context_text


async def delete_side_chat(child_session_id: str) -> bool:
    """Cancel the child turn, purge every store, then remove the parent link."""
    row = side_chat_store.get_side_chat(child_session_id)
    if row is None:
        return False

    from server.service.turn_runner import cancel_session_turn

    await cancel_session_turn(child_session_id)

    try:
        from server.service.browser_manager import get_browser_manager

        await get_browser_manager().close_session(child_session_id)
    except Exception:  # noqa: BLE001 - a dead browser must not block the purge
        logger.debug("side chat: browser page cleanup skipped for {}", child_session_id)

    # A side chat may have spawned subagents / TaskFlow children; clear the
    # whole descendant tree before the child's own stores.
    try:
        from server.service.session_cleanup_service import purge_subagent_subtree_for_session

        await purge_subagent_subtree_for_session(child_session_id)
    except Exception:  # noqa: BLE001 - a broken child tree must not block the purge
        logger.warning("side chat: subagent subtree purge failed for {}", child_session_id)

    from server.service.session_cleanup_service import purge_session_data

    await purge_session_data(child_session_id)
    side_chat_store.delete_side_chat(child_session_id)
    logger.info(
        "side chat purged: child={} parent={}",
        child_session_id,
        row.get("parent_session_id"),
    )
    return True


async def purge_side_chat_children(parent_session_id: str) -> int:
    """Purge every side chat linked to *parent_session_id* (fail-open per child)."""
    removed = 0
    for child_session_id in side_chat_store.list_children(parent_session_id):
        try:
            if await delete_side_chat(child_session_id):
                removed += 1
        except Exception:  # noqa: BLE001 - one child must not block the parent purge
            logger.warning(
                "side chat: child purge failed during parent deletion: {}",
                child_session_id,
                exc_info=True,
            )
    return removed


def side_chat_ids() -> set[str]:
    """Hidden child ids (the normal session list filters these out)."""
    return side_chat_store.side_chat_ids()
