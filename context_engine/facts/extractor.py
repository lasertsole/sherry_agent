"""Auxiliary-LLM extraction of durable facts from a conversation slice."""

from __future__ import annotations

from typing import Any

from loguru import logger

_EXTRACTION_PROMPT = """\
Extract durably useful facts from the following conversation slice (information valuable across sessions).

Extraction rules:
1. User preferences and habits
2. Project conventions and environment facts
3. Key technical decisions and their reasons
4. Tool usage lessons
5. Do not extract temporary task progress

Available categories (category must be one of them): {categories}

Conversation slice:
{conversation_text}

Output a JSON array, each element: {{"category": "...", "fact": "..."}}
When there is nothing worth extracting, output an empty array [].
"""

_VALID_CATEGORIES: tuple[str, ...] = (
    "environment",
    "project",
    "decisions",
    "user_prefs",
    "tool_lessons",
)
_DEFAULT_CATEGORY = "project"


def _get_llm():
    from models import build_auxiliary_llm

    return build_auxiliary_llm()


def _parse_facts(raw: str) -> list[dict[str, str]]:
    """Parse the LLM output into [{category, fact}] with category fallback."""
    import json_repair

    parsed: Any = json_repair.loads(raw)
    if not isinstance(parsed, list):
        # A non-list response is a pipeline failure, not "no facts": raising
        # keeps the watermark unadvanced so the range is replayed (at-least-once).
        raise ValueError(f"facts extraction returned {type(parsed).__name__}, expected list")
    facts: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        fact = str(item.get("fact", "")).strip()
        if not fact:
            continue
        category = str(item.get("category", _DEFAULT_CATEGORY)).strip().lower()
        if category not in _VALID_CATEGORIES:
            category = _DEFAULT_CATEGORY
        facts.append({"category": category, "fact": fact})
    return facts


async def extract_facts(
    conversation_text: str, categories: tuple[str, ...] = _VALID_CATEGORIES
) -> list[dict[str, str]]:
    """Run the extraction prompt over a formatted conversation slice."""
    if not conversation_text.strip():
        return []
    prompt = _EXTRACTION_PROMPT.format(
        categories=", ".join(categories), conversation_text=conversation_text
    )
    response = await _get_llm().ainvoke(prompt)
    raw = getattr(response, "content", "")
    if not isinstance(raw, str):
        raw = str(raw)
    facts = _parse_facts(raw)
    logger.debug("facts extraction: {} fact(s) from {} chars", len(facts), len(conversation_text))
    return facts
