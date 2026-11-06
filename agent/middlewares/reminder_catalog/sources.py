"""Default reminder sources: the live views that used to sit in the system prompt.

Each builder is one of ``workspace.prompt_builder``'s dynamic block builders —
unchanged, so the rendered text is exactly what the prompt used to carry. What
changed is WHERE it lands: a ``<system-reminder>`` in the transcript (refreshed
every turn from live state) instead of a system-prompt block (frozen in the
prompt cache until the next rebuild).

Budgets and priorities follow the catalogue plan: a live view that a turn may
starve for is cheap and low-priority (knowledge index), one the agent is
actively steering by is expensive and high (todo list, plan mode).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage

from workspace.prompt_builder import (
    _build_boulder_block,
    _build_continuity_block,
    _build_knowledge_block,
    _build_plan_mode_block,
    _build_project_dir_block,
    _build_project_instructions_block,
    _build_taskflow_block,
    _build_todo_block,
)

from .core import ReminderChannel, ReminderSource, register

__all__ = ["register_default_sources"]


def _as_builder(builder: Any) -> Any:
    """Adapt a ``(session_id) -> str`` block builder to the source signature."""

    def _source_builder(session_id: str, _state: dict[str, Any]) -> str | None:
        text = builder(session_id)
        return text or None

    return _source_builder


#: English weekday names, independent of the process locale (``strftime("%A")``
#: follows the C locale and can return a localized name).
_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def _local_time_builder(_session_id: str, _state: dict[str, Any]) -> str:
    """One English, local-time line with date, weekday, seconds and UTC offset."""
    now = datetime.now().astimezone()
    offset = now.strftime("%z")
    offset_fmt = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset or "+00:00"
    zone = now.tzname() or "local"
    return (
        f"Current local time: {now.strftime('%Y-%m-%d %H:%M:%S')} "
        f"({_WEEKDAYS[now.weekday()]}) ({zone}, UTC{offset_fmt})"
    )


def _last_user_text(state: dict[str, Any]) -> str:
    """The current turn's user request, excluding injected carriers."""
    messages = state.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, HumanMessage):
            continue
        meta = getattr(message, "metadata", None) or {}
        if isinstance(meta, dict) and meta.get("internal"):
            continue
        content = message.content
        text = content if isinstance(content, str) else str(content)
        text = text.strip()
        if text.startswith("[SYSTEM DIRECTIVE") or text.startswith("<system-reminder"):
            continue
        return text[:2000]
    return ""


async def _memory_recall_builder(session_id: str, state: dict[str, Any]) -> str | None:
    """Per-turn semantic recall, only while the switch is on and a query exists."""
    from agent.tools.memory import memory_store

    if not memory_store.semantic_recall_enabled:
        return None
    query = _last_user_text(state)
    if not query:
        return None
    try:
        from context_engine.store.memory_recall import semantic_recall

        entries = await semantic_recall(
            query,
            limit=memory_store.semantic_recall_top_k,
            char_budget=memory_store.semantic_recall_char_budget,
            session_id=session_id,
            repo=memory_store.repo,
        )
    except Exception:  # noqa: BLE001 - recall is advisory, never load-bearing
        return None
    if not entries:
        return None
    lines = [
        "[MEMORY RECALL — entries relevant to the current request. "
        "Full entries remain available via memory(action='read').]"
    ]
    for entry in entries:
        tags = str(entry.get("target") or "memory")
        if entry.get("stale"):
            tags += ", stale"
        lines.append(
            f"- ({tags}, id {entry.get('id')}, used {entry.get('usage_count', 0)}x, "
            f"score {entry.get('score', 0):.3f}) {entry.get('content', '')}"
        )
    return "\n".join(lines)


def register_default_sources() -> None:
    """Register the live views, semantic recall and the local clock (idempotent)."""
    register(
        ReminderSource(
            source_id="plan_mode_block",
            channel=ReminderChannel.CURRENT_TURN,
            builder=_as_builder(_build_plan_mode_block),
            budget_chars=2000,
            priority=85,
            only_if_changed=True,
        )
    )
    register(
        ReminderSource(
            source_id="todo_block",
            channel=ReminderChannel.CURRENT_TURN,
            builder=_as_builder(_build_todo_block),
            budget_chars=1500,
            priority=80,
            # The todo list is the agent's working plan: never throttled, and
            # re-sent whenever it changed (a stable list is not repeated).
            only_if_changed=True,
        )
    )
    register(
        ReminderSource(
            source_id="taskflow_status",
            channel=ReminderChannel.CURRENT_TURN,
            builder=_as_builder(_build_taskflow_block),
            budget_chars=2000,
            priority=75,
            # A steered board is worth a nudge, but not every single turn: at
            # most once per 5 turns, and only when it actually moved.
            cooldown_turns=5,
            only_if_changed=True,
        )
    )
    register(
        ReminderSource(
            source_id="boulder_status",
            channel=ReminderChannel.CURRENT_TURN,
            builder=_as_builder(_build_boulder_block),
            budget_chars=1000,
            priority=70,
            cooldown_turns=5,
            only_if_changed=True,
        )
    )
    register(
        ReminderSource(
            source_id="continuity_context",
            channel=ReminderChannel.CURRENT_TURN,
            builder=_as_builder(_build_continuity_block),
            budget_chars=1000,
            priority=65,
            only_if_changed=True,
            # Cross-turn continuity is still useful after a compaction; the
            # other live views are re-derived and should not reach the summary.
            compress_exclude=False,
        )
    )
    register(
        ReminderSource(
            source_id="project_dir_notice",
            channel=ReminderChannel.CURRENT_TURN,
            builder=_as_builder(_build_project_dir_block),
            budget_chars=500,
            priority=60,
            only_if_changed=True,
        )
    )
    register(
        ReminderSource(
            source_id="project_instructions",
            channel=ReminderChannel.CURRENT_TURN,
            builder=_as_builder(_build_project_instructions_block),
            budget_chars=8000,
            priority=62,
            only_if_changed=True,
            # 8 k of semi-stable AGENTS.md content: inject it on the turn it
            # changed (directory switch / first turn / post-compaction), then
            # strip it from the message list on later turns. The model reads it
            # once; keeping it resident would waste every subsequent context.
            transient=True,
        )
    )
    register(
        ReminderSource(
            source_id="knowledge_index",
            channel=ReminderChannel.CURRENT_TURN,
            builder=_as_builder(_build_knowledge_block),
            budget_chars=1000,
            priority=50,
            only_if_changed=True,
        )
    )
    # Semantic recall sits ABOVE plan mode: when the switch is on, the entries
    # relevant to the current request outrank the static live views. The
    # builder is async (embedding + reranker); the sync path skips it.
    register(
        ReminderSource(
            source_id="memory_recall",
            channel=ReminderChannel.CURRENT_TURN,
            builder=_memory_recall_builder,
            budget_chars=4000,
            priority=88,
            cooldown_turns=3,
        )
    )
    # Local wall-clock time: always injected, always last in the budget order.
    # ``transient`` strips yesterday's copy from the transcript, so exactly one
    # current line rides the context instead of accumulating one per turn.
    register(
        ReminderSource(
            source_id="current_time",
            channel=ReminderChannel.CURRENT_TURN,
            builder=_local_time_builder,
            budget_chars=120,
            priority=10,
            transient=True,
        )
    )
