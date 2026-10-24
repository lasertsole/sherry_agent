"""Ephemeral side-chat sessions: create, inherit context, purge.

A side chat is a normal session with a parent link (``side_chat_store``), but
its lifecycle is bound to that parent and to the right-sidebar tab. This module
owns the parent transcript snapshot injected as model-only context and the
full purge used by both tab-close and parent deletion.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from loguru import logger

from pub.func.validator import is_safe_session_id
from pub.func.estimate_tokens import estimate_text_tokens
from server.DAO import side_chat_store

__all__ = [
    "build_parent_context",
    "create_side_chat",
    "delete_side_chat",
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


async def create_side_chat(parent_session_id: str) -> dict[str, Any]:
    """Create a child id + parent link + bounded inherited context."""
    if not is_safe_session_id(parent_session_id):
        raise ValueError("invalid parent_session_id")
    if side_chat_store.get_side_chat(parent_session_id) is not None:
        raise ValueError("a side chat cannot parent another side chat")

    child_session_id = uuid.uuid4().hex
    context_text = await build_parent_context(parent_session_id)
    side_chat_store.create_side_chat(child_session_id, parent_session_id, context_text)
    logger.info(
        "side chat created: child={} parent={} context_chars={}",
        child_session_id,
        parent_session_id,
        len(context_text),
    )
    return {
        "session_id": child_session_id,
        "parent_session_id": parent_session_id,
        "inherited": bool(context_text),
    }


def get_context_for_turn(session_id: str) -> str | None:
    """The inherited transcript for a child's FIRST turn, or None afterwards."""
    row = side_chat_store.get_side_chat(session_id)
    if row is None:
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
