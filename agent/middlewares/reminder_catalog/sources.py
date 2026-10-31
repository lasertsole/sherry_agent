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

from typing import Any

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


def _tasks_enabled(session_id: str) -> bool:
    """Whether the session has the full tasks tool group (None = all tools)."""
    from agent.middlewares.tool_selection.core import enabled_tool_names
    from agent.tools.catalog import TOOL_GROUPS

    enabled = enabled_tool_names(session_id)
    return enabled is None or frozenset(TOOL_GROUPS["tasks"]) <= enabled


def _tasks_gated(builder: Any) -> Any:
    """Adapt a builder but skip it when the session's tasks tools are off."""

    def _source_builder(session_id: str, _state: dict[str, Any]) -> str | None:
        if not _tasks_enabled(session_id):
            return None
        text = builder(session_id)
        return text or None

    return _source_builder


def register_default_sources() -> None:
    """Register the seven live views (idempotent — a re-register replaces)."""
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
            builder=_tasks_gated(_build_todo_block),
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
            builder=_tasks_gated(_build_taskflow_block),
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
        )
    )
    register(
        ReminderSource(
            source_id="knowledge_index",
            channel=ReminderChannel.CURRENT_TURN,
            builder=_tasks_gated(_build_knowledge_block),
            budget_chars=1000,
            priority=50,
            only_if_changed=True,
        )
    )
