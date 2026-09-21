"""LLM-based step-result judge for the TaskFlow DAG.

Inspired by hermes-agent's ``judge_goal()``: an auxiliary LLM evaluates whether
a subagent's step result satisfies the step's ``validation_criteria`` and
returns PASS / RETRY / BLOCK.

Design principles:
- Fail-open: any judge failure (disabled, model error, unparseable response)
  degrades to PASS so progress is never blocked by the judge itself.
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

from config.features import STEP_JUDGE
from models import build_auxiliary_llm


class StepVerdict(StrEnum):
    """Judge verdict for a completed TaskFlow step."""

    PASS = "pass"
    RETRY = "retry"
    BLOCK = "block"


class JudgeResult(NamedTuple):
    """A judge verdict plus its rationale and (on RETRY) retry guidance."""

    verdict: StepVerdict
    reason: str
    feedback: str


JUDGE_SYSTEM_PROMPT = """\
You are a strict judge evaluating whether a subagent's result satisfies \
a task step's acceptance criteria. You receive:

1. The step's task description (what was asked)
2. The step's validation_criteria (what constitutes success)
3. The subagent's result text (what was produced)
4. Verification evidence status (test/lint/build pass-fail if available)

Decide one of three verdicts:

PASS — the result satisfies the validation criteria:
- The deliverable was produced and matches the criteria
- OR the result explains that the task is unachievable with clear reasoning

RETRY — the result is incomplete but retryable:
- The subagent produced partial work that could be completed with another attempt
- The result contains an error that could be fixed on retry
- The validation criteria are not yet met but are achievable

BLOCK — the result indicates the step cannot be completed:
- The task is fundamentally unachievable (missing dependency, design conflict)
- The subagent exhausted its retry budget without progress
- Human intervention is required

Reply ONLY with a single JSON object on one line:
{"verdict": "pass", "reason": "<one sentence>"}
{"verdict": "retry", "reason": "<one sentence>", "feedback": "<actionable guidance for retry>"}
{"verdict": "block", "reason": "<one sentence>"}
"""

_VERDICT_PATTERN = re.compile(r'"(?:verdict)"\s*:\s*"(pass|retry|block)"', re.IGNORECASE)


def _build_judge_prompt(
    step_task: str,
    criteria: str | None,
    result_text: str,
    evidence_summary: str | None,
) -> str:
    """Build the user prompt for the step judge."""
    parts = [f"## Step Task\n{step_task}"]
    if criteria:
        parts.append(f"## Validation Criteria\n{criteria}")
    parts.append(f"## Subagent Result\n{result_text}")
    if evidence_summary:
        parts.append(f"## Verification Evidence\n{evidence_summary}")
    else:
        parts.append("## Verification Evidence\n(none recorded)")
    parts.append("## Your Verdict")
    return "\n\n".join(parts)


def _coerce_verdict(value: object) -> StepVerdict | None:
    """Map a raw verdict token to a ``StepVerdict``, or None when unknown."""
    if not isinstance(value, str):
        return None
    try:
        return StepVerdict(value.strip().lower())
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


def _parse_judge_response(raw: str) -> JudgeResult:
    """Parse the judge response, failing open to PASS on any malformed output."""
    data = _load_json_object(raw)
    if data is not None:
        verdict = _coerce_verdict(data.get("verdict"))
        if verdict is not None:
            feedback = str(data.get("feedback") or "") if verdict is StepVerdict.RETRY else ""
            return JudgeResult(verdict, str(data.get("reason") or ""), feedback)

    match = _VERDICT_PATTERN.search(raw)
    if match is not None:
        verdict = _coerce_verdict(match.group(1))
        if verdict is not None:
            return JudgeResult(verdict, "", "")

    logger.warning("StepJudge: no usable verdict in response, failing open as PASS")
    return JudgeResult(StepVerdict.PASS, "judge parse failure (fail-open)", "")


async def judge_step_result(
    step_task: str,
    criteria: str | None,
    result_text: str,
    evidence_summary: str | None = None,
) -> JudgeResult:
    """Judge whether a subagent's step result satisfies its validation criteria.

    Fail-open: any error (disabled judge, model failure, unparseable output)
    degrades to PASS so the judge can never block progress.
    """
    if not STEP_JUDGE["enabled"]:
        return JudgeResult(StepVerdict.PASS, "step judge disabled (fail-open)", "")

    prompt = _build_judge_prompt(
        step_task,
        criteria,
        (result_text or "")[: int(STEP_JUDGE["max_result_chars"])],
        evidence_summary if STEP_JUDGE["evidence_aware"] else None,
    )
    try:
        llm = build_auxiliary_llm(temperature=0)
        response = await llm.ainvoke(
            [
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
        )
        raw = response.content if hasattr(response, "content") else str(response)
        return _parse_judge_response(str(raw))
    except Exception as exc:  # noqa: BLE001 - the fail-open boundary of this judge
        logger.warning("StepJudge failed, failing open as PASS: {}", exc)
        return JudgeResult(StepVerdict.PASS, f"judge error (fail-open): {exc}", "")
