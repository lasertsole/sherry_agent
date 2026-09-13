"""Auxiliary-LLM extraction of durable facts from a conversation slice."""

from __future__ import annotations

from typing import Any

from loguru import logger

_EXTRACTION_PROMPT = """\
从以下对话片段中提取持久有用的事实（跨会话有价值的信息）。

提取规则:
1. 用户偏好和习惯
2. 项目约定和环境事实
3. 关键技术决策和原因
4. 工具使用经验
5. 不提取临时任务进度

可用类别（category 必须是其中之一）: {categories}

对话片段:
{conversation_text}

输出 JSON 数组，每个元素: {{"category": "...", "fact": "..."}}
没有值得提取的事实时输出空数组 []。
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
