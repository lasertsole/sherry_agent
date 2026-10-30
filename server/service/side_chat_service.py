"""Side-chat sessions: create, inherit context, purge.

A side chat is a normal session with a parent link (``side_chat_store``), and
its lifecycle is bound to that parent — closing the right-sidebar panel keeps
the child and every byte of its state; deleting the parent purges it. It is a
COMPLETE fork of the parent agent: its first turn is seeded with the parent's
checkpoint transcript and the parent's per-session settings, while the copied
messages carry a model-only marker so the side chat's own visible history stays
empty. This module also owns the bounded text fallback for links created before
the checkpoint fork and the full purge used by parent deletion.
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
    "list_session_links",
    "purge_side_chat_children",
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
#: (same model, thinking, agent face, directory, access mode and budget). The
#: two announced baselines ride along with the directory: the child starts in
#: the root the parent already told its agent about, so its first turn must not
#: replay a directory/git change notice the parent never saw.
_SIDE_CHAT_INHERITED_KEYS = (
    StateKey.LLM_MAIN_MODEL,
    StateKey.LLM_THINKING_ENABLED,
    StateKey.CUSTOM_THINKING_MAPPING,
    StateKey.SEARCH_BACKEND,
    StateKey.TOKEN_BUDGET_CAP,
    StateKey.AGENT_CONFIG,
    StateKey.PROJECT_DIR,
    StateKey.PROJECT_DIR_ANNOUNCED,
    StateKey.GIT_HEAD_ANNOUNCED,
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


def _history_up_to_turn(messages: list[BaseMessage], turn_num: int | None) -> list[BaseMessage]:
    """Cut a parent transcript at a fork point.

    ``turn_num is None`` keeps the legacy side-chat rule (the last completed,
    call-less assistant answer). ``turn_num=N`` cuts after the Nth USER turn's
    last call-less assistant answer — the branch fork point.

    Turn boundaries are counted over non-internal ``HumanMessage``s: reminder
    blocks and workspace notices are injected humans that belong to the turn
    they precede, and the client's ``turn_num`` (which the fork button sends)
    never counts them.

    Returns ``[]`` when turn N is not in the transcript (compacted away); the
    caller then falls back to the bounded text snapshot. A turn whose answer was
    still mid-tool when it was cut off has no safe assistant boundary, so the
    fork stops at the turn's FIRST message: replaying the parent's dangling tool
    call would make the child's first turn execute it.
    """
    if turn_num is None:
        return _completed_history(messages)

    human_count = 0
    turn_start: int | None = None
    turn_end = len(messages)
    for index, message in enumerate(messages):
        if message.type != "human":
            continue
        metadata = getattr(message, "metadata", None) or {}
        if metadata.get("internal") is True:
            continue
        human_count += 1
        if human_count == turn_num:
            turn_start = index
        elif human_count == turn_num + 1:
            turn_end = index
            break
    if turn_start is None:
        return []

    for offset in range(turn_end - turn_start - 1, -1, -1):
        message = messages[turn_start + offset]
        if isinstance(message, AIMessage) and not getattr(message, "tool_calls", None):
            return list(messages[: turn_start + offset + 1])
    return list(messages[:turn_start])


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
    fork_turn_num = row.get("fork_turn_num")
    parent_messages: list[BaseMessage] = []
    if parent_session_id:
        try:
            from agent.state_port import read_messages

            parent_messages = _history_up_to_turn(
                list(await read_messages(parent_session_id)),
                int(fork_turn_num) if fork_turn_num is not None else None,
            )
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


def _resolve_fork_turn(parent_session_id: str, fork_turn_num: int) -> int | None:
    """Map a clicked row's persisted turn to the parent's USER-turn ordinal.

    The client sends the AI answer row's ``turn_num`` — and a persisted
    ``turn_num`` is a per-BATCH number (one user turn spans several batches when
    the agent makes tool rounds: user=1, answer=3 …). The checkpoint cut counts
    USER turns, so the two must be translated: ``find_user_turn_cut`` (the same
    helper the message edit uses) answers the ordinal of the user row at or
    before that turn. ``None`` = the row is no longer persisted (compacted
    away): the caller then keeps the legacy last-answer cut.
    """
    from context_engine.store.core import find_user_turn_cut

    try:
        cut = find_user_turn_cut(parent_session_id, fork_turn_num)
    except Exception as exc:  # noqa: BLE001 - a lookup failure degrades, never blocks
        logger.warning("side chat: fork-turn lookup failed for {}: {}", parent_session_id, exc)
        return None
    if cut is None:
        return None
    return int(cut[0])


def _validated_fork_turn(fork_turn_num: int | None) -> int | None:
    """Validate a branch fork point (a 1-based parent turn), or ``None``."""
    if fork_turn_num is None:
        return None
    if isinstance(fork_turn_num, bool):  # bools are ints; a flag is not a turn
        raise ValueError("invalid fork_turn_num")
    try:
        value = int(fork_turn_num)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid fork_turn_num") from exc
    if value < 1:
        raise ValueError("invalid fork_turn_num")
    return value


async def create_side_chat(
    parent_session_id: str,
    client_request_id: str | None = None,
    fork_turn_num: int | None = None,
) -> dict[str, Any]:
    """Create a child id + parent link + checkpoint-fork inheritance.

    ``client_request_id`` (design concept #27) makes the creation IDEMPOTENT:
    a retried POST — a timed-out response, a re-click after a network blip —
    returns the child the first attempt created instead of building a second
    complete fork. Without one the legacy path applies (every call forks).

    ``fork_turn_num`` turns the same creation into a BRANCH: the child inherits
    the parent transcript up to that USER turn instead of the parent's last
    answer. It arrives as the clicked row's persisted turn number (a per-batch
    number) and is translated to the checkpoint's user-turn ordinal before it is
    stored — the stored value is what the cut consumes, so a restart re-derives
    the same point. ``None`` keeps a side chat's behaviour byte-for-byte.
    """
    if not is_safe_session_id(parent_session_id):
        raise ValueError("invalid parent_session_id")
    fork_turn_num = _validated_fork_turn(fork_turn_num)
    if fork_turn_num is not None:
        resolved = await asyncio.to_thread(_resolve_fork_turn, parent_session_id, fork_turn_num)
        if resolved is None:
            logger.info(
                "side chat: fork turn {} has no persisted user row for {}; "
                "falling back to the last completed answer",
                fork_turn_num,
                parent_session_id,
            )
        fork_turn_num = resolved
    if side_chat_store.get_side_chat(parent_session_id) is not None:
        raise ValueError("a side chat cannot parent another side chat")

    request_id = (client_request_id or "").strip() or None
    if request_id:
        existing = side_chat_store.find_by_request(parent_session_id, request_id)
        if existing is not None:
            logger.info(
                "side chat deduplicated: child={} parent={} request_id={}",
                existing["child_session_id"],
                parent_session_id,
                request_id,
            )
            return {
                "session_id": str(existing["child_session_id"]),
                "parent_session_id": parent_session_id,
                "fork_turn_num": existing.get("fork_turn_num"),
                "inherited": True,
                "deduplicated": True,
            }

    child_session_id = uuid.uuid4().hex
    context_text = await build_parent_context(parent_session_id)
    try:
        side_chat_store.create_side_chat(
            child_session_id,
            parent_session_id,
            context_text,
            fork_mode=True,
            client_request_id=request_id,
            fork_turn_num=fork_turn_num,
        )
    except Exception:
        # Two concurrent creators with the same request id: the unique index
        # rejected the loser — answer the winner's child, never a 500.
        if request_id and (
            existing := side_chat_store.find_by_request(parent_session_id, request_id)
        ):
            logger.info(
                "side chat deduplicated (concurrent): child={} request_id={}",
                existing["child_session_id"],
                request_id,
            )
            return {
                "session_id": str(existing["child_session_id"]),
                "parent_session_id": parent_session_id,
                "fork_turn_num": existing.get("fork_turn_num"),
                "inherited": True,
                "deduplicated": True,
            }
        raise
    copied = _copy_parent_session_settings(parent_session_id, child_session_id)
    logger.info(
        "side chat created: child={} parent={} fork_turn={} context_chars={} settings={}",
        child_session_id,
        parent_session_id,
        fork_turn_num,
        len(context_text),
        copied,
    )
    return {
        "session_id": child_session_id,
        "parent_session_id": parent_session_id,
        "fork_turn_num": fork_turn_num,
        "inherited": True,
        "deduplicated": False,
    }


def list_session_links() -> list[dict[str, Any]]:
    """Every parent link, for the client's branch marks (one small table)."""
    return [
        {
            "child_session_id": str(row.get("child_session_id") or ""),
            "parent_session_id": str(row.get("parent_session_id") or ""),
            "fork_turn_num": row.get("fork_turn_num"),
        }
        for row in side_chat_store.all_side_chats()
    ]


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
