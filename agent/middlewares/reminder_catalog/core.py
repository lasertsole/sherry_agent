"""The reminder catalog: one registry for the dynamic ``<system-reminder>`` layer.

Why this exists: the live views an agent needs (todo list, TaskFlow board,
boulder status, continuity context, knowledge index, project directory) used to
be rebuilt into the SYSTEM PROMPT on every call. That made the prompt change
every turn — and a prompt whose first token differs invalidates the provider's
prefix cache for everything after it, persona included. They are now injected
into the transcript as reminders instead, and this module is what decides when,
whether, and how much:

* **register** — each source declares a builder (session_id, state → text | None)
  plus its budget, priority, cooldown and change-detection switch;
* **resolve_injections** — runs the sources for one model call and returns the
  text to inject, after the cooldown, change and total-budget checks;
* **reset_session** — drops a session's throttle state, so the turn after a
  compression re-injects every source from live state (the compression may have
  dropped the older reminders).

State is process-local and per session; losing it costs one extra injection, it
never silently withholds content that should have been shown twice.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from loguru import logger

from config.features import REMINDER_CATALOG

__all__ = [
    "ReminderChannel",
    "ReminderSource",
    "clear_session",
    "registered_sources",
    "register",
    "aresolve_injections",
    "resolve_injections",
    "transient_source_ids",
]


class ReminderChannel(StrEnum):
    """Where a reminder may be delivered.

    Only ``CURRENT_TURN`` is implemented today (a ``before_model`` injection on
    the turn's first model call); the enum names the other channels a source may
    declare so the registry can refuse a channel nothing consumes yet instead of
    silently dropping the content.
    """

    SYSTEM_PROMPT = "system_prompt"
    CURRENT_TURN = "current_turn"
    TOOL_RESULT = "tool_result"
    HISTORY_CONTINUITY = "history_continuity"
    MID_TURN_EVENT = "mid_turn_event"
    REAL_USER = "real_user"


@dataclass(frozen=True)
class ReminderSource:
    """One injection point's declaration.

    :param source_id: Stable identifier (also the message's ``origin`` and the
        ``source=`` attribute in the tag).
    :param channel: Delivery channel (only ``CURRENT_TURN`` is consumed today).
    :param builder: ``(session_id, state) -> text | None`` (or an awaitable of
        either); ``None``/blank means "nothing to say this turn". Async builders
        are awaited by ``aresolve_injections`` (the production path) and skipped
        by the sync path.
    :param budget_chars: Per-source cap; longer text is clipped with an ellipsis.
    :param priority: Higher wins under the total budget; 100 = must keep.
    :param cooldown_turns: Inject at most once per N turns (0 = no cooldown).
    :param only_if_changed: Skip when the rendered text is identical to the last
        text this session injected for the source.
    :param transient: When True, the reminder is stripped from the message list
        on turns it is not re-resolved, so it only occupies context on the turn
        its content changed (and the first turn after a compaction). Use for
        large, semi-stable payloads (project_instructions).
    :param compress_exclude: When True, a summarization pass drops this
        reminder before feeding the messages to the summarizer. Reminders are
        re-derived from live state every turn, so an old snapshot is noise in a
        summary; a source that carries cross-turn continuity opts out.
    """

    source_id: str
    channel: ReminderChannel
    builder: Callable[[str, dict[str, Any]], str | None | Awaitable[str | None]]
    budget_chars: int = 2000
    priority: int = 50
    cooldown_turns: int = REMINDER_CATALOG["default_cooldown_turns"]
    only_if_changed: bool = False
    is_meta: bool = True
    tags: tuple[str, ...] = field(default_factory=tuple)
    transient: bool = False
    compress_exclude: bool = True


#: Registered sources by id (registration order is not significant: the
#: resolution sorts by priority).
_SOURCES: dict[str, ReminderSource] = {}

#: ``{session_id: {source_id: {"turn": int, "hash": str}}}`` — the throttle and
#: change-detection memory. Updated ONLY when a source's text is actually
#: injected (a source dropped for budget must not be marked as shown).
_THROTTLE_STATE: dict[str, dict[str, dict[str, int | str]]] = {}


def register(source: ReminderSource) -> None:
    """Add a source to the catalog (re-registering an id replaces it)."""
    if source.channel is not ReminderChannel.CURRENT_TURN:
        # Nothing consumes the other channels yet: refuse loudly instead of
        # registering a source whose content would never reach the model.
        raise ValueError(
            f"reminder_catalog: source {source.source_id!r} declares channel "
            f"{source.channel!r}, which has no consumer yet"
        )
    _SOURCES[source.source_id] = source


def registered_sources() -> dict[str, ReminderSource]:
    """The registered sources (a copy — callers must not mutate the registry)."""
    return dict(_SOURCES)


def transient_source_ids() -> frozenset[str]:
    """Source ids whose reminders must not persist beyond their injection turn."""
    return frozenset(src.source_id for src in _SOURCES.values() if src.transient)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _is_due(source: ReminderSource, shown: dict[str, Any], current_turn: int) -> bool:
    """Whether the source's cooldown allows an injection this turn."""
    if source.cooldown_turns <= 0 or not shown:
        return True
    # The cooldown applies only to a source that HAS been injected:
    # "never shown" is eligible now, not cooling down from turn 0.
    last_turn = int(shown.get("turn", 0) or 0)
    return current_turn - last_turn >= source.cooldown_turns


def _accept_candidate(
    source: ReminderSource,
    content: str | None,
    shown: dict[str, Any],
    candidates: list[tuple[ReminderSource, str]],
) -> None:
    """Clip, change-check and append one builder result."""
    if not content or not content.strip():
        return
    if len(content) > source.budget_chars:
        content = content[: source.budget_chars] + "…"
    if source.only_if_changed and _content_hash(content) == shown.get("hash"):
        return
    candidates.append((source, content))


def _apply_budget(
    candidates: list[tuple[ReminderSource, str]],
    session_id: str,
    current_turn: int,
) -> dict[str, str]:
    """Sort by priority, spend the total budget, and record what was kept."""
    candidates.sort(key=lambda item: -item[0].priority)
    result: dict[str, str] = {}
    total = 0
    for source, content in candidates:
        if total + len(content) > REMINDER_CATALOG["total_budget_chars"]:
            logger.debug(
                "reminder_catalog: budget exceeded, skipped {} ({} chars, {} so far)",
                source.source_id,
                len(content),
                total,
            )
            continue
        result[source.source_id] = content
        total += len(content)
        _THROTTLE_STATE.setdefault(session_id, {})[source.source_id] = {
            "turn": current_turn,
            "hash": _content_hash(content),
        }
    return result


def resolve_injections(
    session_id: str,
    state: dict[str, Any],
    *,
    current_turn: int,
) -> dict[str, str]:
    """Resolve every source for one model call (sync sources only).

    Applies, in order: the enabled switch → the per-source cooldown → the
    builder → the change check → the per-source cap → the TOTAL budget (by
    priority, dropping whole sources rather than truncating a source to nothing)
    → the throttle bookkeeping for what was actually kept.

    Async builders are skipped here; the production path is
    :func:`aresolve_injections`.
    """
    if not REMINDER_CATALOG["enabled"]:
        return {}
    session_state = _THROTTLE_STATE.get(session_id, {})
    candidates: list[tuple[ReminderSource, str]] = []
    for source in _SOURCES.values():
        shown = session_state.get(source.source_id, {})
        if not _is_due(source, shown, current_turn):
            continue
        if inspect.iscoroutinefunction(source.builder):
            logger.warning(
                "reminder_catalog: async builder {} skipped on the sync path",
                source.source_id,
            )
            continue
        try:
            content = source.builder(session_id, state)
        except Exception:  # noqa: BLE001 - one broken source must not kill the layer
            logger.exception("reminder_catalog: builder failed for {}", source.source_id)
            continue
        if inspect.isawaitable(content):
            # A sync wrapper returned a coroutine: close it so the GC does not
            # warn about an un-awaited coroutine.
            close = getattr(content, "close", None)
            if callable(close):
                close()
            logger.warning(
                "reminder_catalog: async builder {} skipped on the sync path",
                source.source_id,
            )
            continue
        _accept_candidate(source, content, shown, candidates)
    return _apply_budget(candidates, session_id, current_turn)


async def aresolve_injections(
    session_id: str,
    state: dict[str, Any],
    *,
    current_turn: int,
) -> dict[str, str]:
    """Async variant: awaits async builders (semantic memory recall)."""
    if not REMINDER_CATALOG["enabled"]:
        return {}
    session_state = _THROTTLE_STATE.get(session_id, {})
    candidates: list[tuple[ReminderSource, str]] = []
    for source in _SOURCES.values():
        shown = session_state.get(source.source_id, {})
        if not _is_due(source, shown, current_turn):
            continue
        try:
            content = source.builder(session_id, state)
            if inspect.isawaitable(content):
                content = await content
        except Exception:  # noqa: BLE001 - one broken source must not kill the layer
            logger.exception("reminder_catalog: builder failed for {}", source.source_id)
            continue
        _accept_candidate(source, content, shown, candidates)
    return _apply_budget(candidates, session_id, current_turn)


def clear_session(session_id: str) -> None:
    """Drop a session's throttle state (session teardown, tests).

    Also the post-compression reset: with the state gone, the next turn
    re-injects every source whose live content still says something — which is
    how reminders survive a compression that discarded the older ones.
    """
    _THROTTLE_STATE.pop(session_id, None)
