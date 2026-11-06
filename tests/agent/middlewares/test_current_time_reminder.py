"""The lowest-priority local-clock reminder."""

from __future__ import annotations

import re

from agent.middlewares.reminder_catalog import (
    clear_session,
    registered_sources,
    resolve_injections,
)
from agent.middlewares.reminder_catalog.sources import (
    _local_time_builder,
    register_default_sources,
)

_TIME_RE = re.compile(
    r"^Current local time: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} "
    r"\((Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\) "
    r"\(.+ UTC[+-]\d{2}:\d{2}\)$"
)


def test_builder_is_english_local_time_with_seconds() -> None:
    text = _local_time_builder("session", {})

    assert _TIME_RE.match(text), text
    assert "年" not in text and "月" not in text and "日" not in text


def test_source_is_lowest_priority_and_injects_every_turn() -> None:
    register_default_sources()
    sources = registered_sources()
    source = sources["current_time"]

    assert source.priority == min(entry.priority for entry in sources.values())
    assert source.cooldown_turns == 0
    assert source.transient is True

    clear_session("s-current-time")
    first = resolve_injections("s-current-time", {}, current_turn=1)
    second = resolve_injections("s-current-time", {}, current_turn=2)

    assert "current_time" in first
    assert "current_time" in second
    assert _TIME_RE.match(first["current_time"])
