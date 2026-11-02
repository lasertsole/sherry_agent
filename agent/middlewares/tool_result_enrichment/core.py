"""Tool-result enrichment: a registry of injectors that append hints to ToolMessage content.

Modelled on the reminder catalog (``agent/middlewares/reminder_catalog``), but
the trigger is each tool call's return instead of the turn's first model call.
An injector receives ``(tool_name, tool_args, result, session_id)`` and returns
optional text; the middleware appends all non-None outputs to the result's
content, sorted by priority (low first, so high-priority hints land at the
bottom — closest to the model's attention).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import ToolMessage
from loguru import logger

__all__ = [
    "ToolResultInjector",
    "clear_session",
    "register",
    "registered_injectors",
    "resolve_injections",
]


@dataclass(frozen=True)
class ToolResultInjector:
    """One enrichment source's declaration.

    :param injector_id: Stable identifier (used for logging and change detection).
    :param builder: ``(tool_name, tool_args, result, session_id) -> str | None``;
        ``None``/blank means "nothing to append for this call".
    :param priority: Lower appended first (top of the suffix block), higher
        appended last (bottom — closest to the model's attention). 100 = highest.
    :param tools_filter: ``None`` = all tools; a set limits the injector to
        those tool names only.
    :param only_if_changed: Skip when the text is identical to the last text
        this injector produced for the same tool in this session.
    """

    injector_id: str
    builder: Callable[[str, dict[str, Any], ToolMessage, str], str | None]
    priority: int = 50
    tools_filter: frozenset[str] | None = None
    only_if_changed: bool = False


_INJECTORS: dict[str, ToolResultInjector] = {}

#: ``{session_id: {injector_id: {tool_name: "hash"}}}`` — change-detection memory
#: for ``only_if_changed`` injectors.
_CHANGE_STATE: dict[str, dict[str, dict[str, str]]] = {}


def register(injector: ToolResultInjector) -> None:
    """Add an injector to the registry (re-registering an id replaces it)."""
    _INJECTORS[injector.injector_id] = injector


def registered_injectors() -> dict[str, ToolResultInjector]:
    """The registered injectors (a copy — callers must not mutate)."""
    return dict(_INJECTORS)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def resolve_injections(
    tool_name: str,
    tool_args: dict[str, Any],
    result: ToolMessage,
    session_id: str,
) -> list[str]:
    """Resolve every injector for one tool call, in append order (low priority first).

    :returns: Non-blank texts to append, ordered so the highest priority is last.
    """
    session_state = _CHANGE_STATE.get(session_id, {})
    candidates: list[tuple[ToolResultInjector, str]] = []

    for injector in _INJECTORS.values():
        if injector.tools_filter is not None and tool_name not in injector.tools_filter:
            continue
        try:
            text = injector.builder(tool_name, tool_args, result, session_id)
        except Exception:  # noqa: BLE001 - one broken injector must not kill the result
            logger.exception("tool_result_enrichment: builder failed for {}", injector.injector_id)
            continue
        if not text or not text.strip():
            continue
        if injector.only_if_changed:
            key_state = session_state.get(injector.injector_id, {})
            current_hash = _content_hash(f"{tool_name}:{text}")
            if current_hash == key_state.get(tool_name):
                continue
            key_state[tool_name] = current_hash
            _CHANGE_STATE.setdefault(session_id, {})[injector.injector_id] = key_state
        candidates.append((injector, text))

    candidates.sort(key=lambda item: item[0].priority)
    return [text for _, text in candidates]


def clear_session(session_id: str) -> None:
    """Drop a session's change-detection state (session teardown, tests)."""
    _CHANGE_STATE.pop(session_id, None)
