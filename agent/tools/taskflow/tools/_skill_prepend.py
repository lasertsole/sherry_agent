"""Prepend bound skills' SKILL.md content to a taskflow step's task text.

A step may declare ``skills: ["browser", "code-wiki"]``: when it is dispatched,
each skill's full SKILL.md is placed in FRONT of the step's task text, so the
child agent receives the skill's instructions directly. The main agent therefore
never has to ``skill_view`` a skill just to relay it into a task string (which
cost ~2 000 tokens of orchestrator context per binding and went through one LLM
paraphrase).

Rendering mirrors cron's ``_assemble_skill_prompt`` (the same
``[IMPORTANT: ...]`` header, the same fail-open binding handling, the same
``bump_use`` telemetry and injection scan): a skill that cannot be loaded is
logged and skipped, never a reason to fail the dispatch.
"""

from __future__ import annotations

import json

from loguru import logger

__all__ = ["prepend_skills"]


def prepend_skills(task: str, skills: list[str] | None) -> str:
    """Place each bound skill's SKILL.md before *task*.

    Returns *task* unchanged when ``skills`` is None/empty or every binding
    fails to load (fail-open: a stale binding — the skill was deleted or
    renamed — degrades to "the step runs without it", never a failed dispatch).
    """
    if not skills:
        return task

    # Lazy imports: the tool layer imports the skills loader, and this module is
    # reached from the dispatch path where that graph is already live.
    from agent.tools.pub_base.skill_usage import bump_use
    from agent.tools.skill_tools.skill_view import _INJECTION_PATTERNS, _skill_view

    blocks: list[str] = []
    for name in skills:
        try:
            parsed = json.loads(_skill_view(name, caller_scope="background"))
        except Exception as exc:  # noqa: BLE001 - a bad binding must not fail the dispatch
            logger.warning("taskflow: failed to load skill '{}': {}", name, exc)
            continue
        if not parsed.get("success"):
            logger.warning(
                "taskflow: skill '{}' is not loadable ({}); the step runs without it",
                name,
                parsed.get("error", "unknown error"),
            )
            continue
        content = parsed.get("content", "")
        if not content:
            logger.warning("taskflow: skill '{}' loaded empty content; skipping", name)
            continue
        blocks.append(
            f'[IMPORTANT: The user has invoked the "{name}" skill. '
            f"Follow the skill's instructions below.]\n\n{content}"
        )
        try:
            bump_use(name)
        except Exception as exc:  # noqa: BLE001 - telemetry is best-effort
            logger.debug("taskflow: bump_use failed for '{}': {}", name, exc)

    if not blocks:
        return task

    logger.info(
        "taskflow: pre-loaded {} bound skill(s) into a step task: {}",
        len(blocks),
        ", ".join(skills),
    )
    assembled = "\n\n".join(blocks) + "\n\n" + task

    # Assembled-prompt scan (cron parity): skill content was vetted at install
    # time, so this is a warning layer only — the step always dispatches.
    lowered = assembled.lower()
    if any(pattern in lowered for pattern in _INJECTION_PATTERNS):
        logger.warning(
            "taskflow: assembled step prompt (skills + task) contains patterns "
            "that may indicate prompt injection"
        )
    return assembled
