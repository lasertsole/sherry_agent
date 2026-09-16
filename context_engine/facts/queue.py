"""Turn-queue orchestration: enqueue persisted turns, consume pending ranges."""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from context_engine import get_turns_by_turn_num_scope
from context_engine.facts import cursor
from context_engine.facts.extractor import extract_facts

if TYPE_CHECKING:
    from runtime.data_provider import PromptDataProvider

# First miss per provider method is logged once (mirrors runtime.hooks consumers).
_provider_misses_logged: set[str] = set()


def _resolve_provider(method: str) -> PromptDataProvider | None:
    """Resolve the prompt data provider, logging the first miss per method.

    The provider is registered by ``agent.core.init()`` at server boot; a
    process that never assembled the agent cannot persist facts and leaves the
    pending range untouched instead of importing across package boundaries.
    """
    from runtime import data_provider

    provider = data_provider.get_prompt_data_provider()
    if provider is None and method not in _provider_misses_logged:
        _provider_misses_logged.add(method)
        logger.debug(
            "facts queue: prompt data provider is not registered; '{}' degrades to a no-op",
            method,
        )
    return provider


async def enqueue_turn(session_id: str, turn_num: int) -> None:
    """Mark a persisted turn as awaiting facts extraction."""
    cursor.advance_enqueued(session_id, turn_num)


def _format_range(rows: list[dict]) -> str:
    lines: list[str] = []
    for row in sorted(rows, key=lambda r: (r.get("turn_num", 0), r.get("id", 0))):
        role = "user" if row.get("role") == "human" else "agent"
        content = row.get("content")
        if not isinstance(content, str):
            content = str(content)
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def process_pending(session_id: str, tiered_store=None) -> int:
    """Extract facts for every turn between the two watermarks.

    Writes through the tiered facts store (injected in tests, otherwise
    resolved through the prompt data provider) and advances the consumed
    watermark only after a successful extraction (crash between the two
    replays the range — at-least-once semantics). Without an injected store and
    without a registered provider the range is left pending and 0 is returned.
    """
    start, end = cursor.get_pending(session_id)
    if end < start:
        return 0

    store = tiered_store
    if store is None:
        provider = _resolve_provider("add_fact")
        if provider is None:
            return 0
        store = provider

    written = 0
    middle = (start + end) // 2
    half_scope = max(1, (end - start + 1))
    rows = get_turns_by_turn_num_scope(
        session_id, target_turn_num=middle, half_scope=half_scope, only_eligible=False
    )
    rows = [r for r in rows if start <= r.get("turn_num", 0) <= end]
    conversation = _format_range(rows)

    facts = await extract_facts(conversation)
    for fact in facts:
        result = store.add_fact(fact["category"], fact["fact"])
        if result.get("success", False):
            written += 1

    cursor.advance_consumed(session_id, end)
    return written
