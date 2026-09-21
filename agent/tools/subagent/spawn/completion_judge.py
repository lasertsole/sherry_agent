"""LLM-based completion judge for subagent runs (goal loop).

Inspired by hermes-agent's goal loop: after a subagent's turn, an auxiliary LLM
evaluates whether the work is truly done. A CONTINUE verdict carries a
continuation prompt that is injected as the subagent's next human turn, and the
run continues until the judge returns DONE or the turn budget is spent.

Unlike the TaskFlow step judge (``agent/tools/taskflow/step_judge.py``), which
judges one step result AFTER ``taskflow_resume`` injects it, this judge
evaluates the whole subagent run BEFORE ``complete_subagent_run()`` finalizes it.

Design principles:
- Fail-open: any judge failure (disabled, model error, unparseable response)
  degrades to DONE so a subagent is never trapped in the goal loop by the judge
  itself.
- Auxiliary LLM at temperature 0 for deterministic verdicts.
- Evidence-aware: the judge is shown the verification-evidence summary.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import NamedTuple

import json_repair
from loguru import logger

from config.features import COMPLETION_JUDGE
from models import build_auxiliary_llm


class CompletionVerdict(StrEnum):
    """Judge verdict for a subagent run."""

    DONE = "done"
    CONTINUE = "continue"


class CompletionJudgeResult(NamedTuple):
    """A completion verdict plus its rationale and (on CONTINUE) next-turn guidance."""

    verdict: CompletionVerdict
    reason: str
    continuation_prompt: str


COMPLETION_JUDGE_SYSTEM_PROMPT = """\
You are a strict judge evaluating whether an autonomous subagent has \
completed its assigned task. You receive:

1. The task description (what was asked of the subagent)
2. The subagent's most recent response (what it produced)
3. Verification evidence status (test/lint/build pass-fail if available)

Decide one of two verdicts:

DONE — the task is fully satisfied:
- The deliverable was produced and matches the task description
- OR the response explains the task is unachievable with clear reasoning

CONTINUE — not done, and there is a concrete next step:
- The subagent produced partial work that needs more turns
- A verification step (test/build/lint) was not run
- An error occurred that could be fixed with another attempt

Reply ONLY with a single JSON object on one line:
{"verdict": "done", "reason": "<one sentence>"}
{"verdict": "continue", "reason": "<one sentence>", "continuation_prompt": "<one paragraph: what the subagent should do next>"}
"""

_VERDICT_PATTERN = re.compile(r'"(?:verdict)"\s*:\s*"(done|continue)"', re.IGNORECASE)

# Truncate the child's answer so a pathologically long one cannot blow the judge's context window.
_MAX_RESPONSE_CHARS = 8000


def _build_completion_prompt(
    task_text: str,
    last_response: str,
    evidence_summary: str | None,
) -> str:
    """Build the user prompt for the completion judge."""
    parts = [f"## Task\n{task_text}", f"## Subagent Response\n{last_response}"]
    if evidence_summary:
        parts.append(f"## Verification Evidence\n{evidence_summary}")
    else:
        parts.append("## Verification Evidence\n(none recorded)")
    parts.append("## Your Verdict")
    return "\n\n".join(parts)


def _coerce_verdict(value: object) -> CompletionVerdict | None:
    """Map a raw verdict token to a ``CompletionVerdict``, or None when unknown."""
    if not isinstance(value, str):
        return None
    try:
        return CompletionVerdict(value.strip().lower())
    except ValueError:
        return None


def _load_json_object(raw: str) -> dict | None:
    """Recover a JSON object from a possibly malformed model response."""
    if not raw.strip():
        return None
    try:
        parsed = json_repair.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if isinstance(parsed, dict):
        return parsed
    # Some models wrap the object in a JSON array; take the first object.
    if isinstance(parsed, list):
        return next((item for item in parsed if isinstance(item, dict)), None)
    return None


def _parse_completion_response(raw: str) -> CompletionJudgeResult:
    """Parse the judge response, failing open to DONE on any malformed output."""
    data = _load_json_object(raw)
    if data is not None:
        verdict = _coerce_verdict(data.get("verdict"))
        if verdict is not None:
            continuation = (
                str(data.get("continuation_prompt") or "")
                if verdict is CompletionVerdict.CONTINUE
                else ""
            )
            return CompletionJudgeResult(verdict, str(data.get("reason") or ""), continuation)

    match = _VERDICT_PATTERN.search(raw)
    if match is not None:
        verdict = _coerce_verdict(match.group(1))
        if verdict is not None:
            return CompletionJudgeResult(verdict, "", "")

    logger.warning("CompletionJudge: no usable verdict in response, failing open as DONE")
    return CompletionJudgeResult(CompletionVerdict.DONE, "judge parse failure (fail-open)", "")


async def judge_completion(
    task_text: str,
    last_response: str,
    evidence_summary: str | None = None,
) -> CompletionJudgeResult:
    """Judge whether a subagent run is complete.

    Fail-open: any error (disabled judge, model failure, unparseable output)
    degrades to DONE so the judge can never trap a subagent in a goal loop.
    """
    if not COMPLETION_JUDGE["enabled"]:
        return CompletionJudgeResult(
            CompletionVerdict.DONE, "completion judge disabled (fail-open)", ""
        )

    prompt = _build_completion_prompt(
        task_text,
        (last_response or "")[:_MAX_RESPONSE_CHARS],
        evidence_summary,
    )
    try:
        llm = build_auxiliary_llm(temperature=0)
        response = await llm.ainvoke(
            [
                {"role": "system", "content": COMPLETION_JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
        )
        raw = response.content if hasattr(response, "content") else str(response)
        return _parse_completion_response(str(raw))
    except Exception as exc:  # noqa: BLE001 - the fail-open boundary of this judge
        logger.warning("CompletionJudge failed, failing open as DONE: {}", exc)
        return CompletionJudgeResult(CompletionVerdict.DONE, f"judge error (fail-open): {exc}", "")
