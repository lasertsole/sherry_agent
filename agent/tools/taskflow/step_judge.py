"""LLM-based step-result judge for the TaskFlow DAG.

Inspired by hermes-agent's ``judge_goal()``: an auxiliary LLM evaluates whether
a subagent's step result satisfies the step's ``validation_criteria`` and
returns PASS / RETRY / BLOCK.

Design principles:
- Judge OUTAGES are reported as ``UNKNOWN``, never as a model-issued PASS. The
  consumer (``taskflow_resume``) maps UNKNOWN to ``block`` by default so an
  unverified result cannot be marked ``done`` and silently unlock dependents —
  the judge failing must not look like the judge approving. ``on_judge_error``
  in ``STEP_JUDGE`` restores the legacy fail-open PASS when explicitly set.
  A model that DOES answer but yields no usable verdict is an outage too.
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
from models import build_taskflow_llm


class StepVerdict(StrEnum):
    """Judge verdict for a completed TaskFlow step."""

    PASS = "pass"
    RETRY = "retry"
    BLOCK = "block"
    UNKNOWN = "unknown"
    """The judge itself failed (provider error / unparseable output). Not a
    model-issued verdict: the consumer decides whether to block or pass via
    ``STEP_JUDGE['on_judge_error']``."""


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

    logger.warning("StepJudge: no usable verdict in response, returning UNKNOWN")
    return JudgeResult(StepVerdict.UNKNOWN, "judge parse failure (no usable verdict)", "")


# ── Optional self-evaluation mode (WorkMode stage 3) ────────────────────────
#
# The default judge stays a three-value PASS/RETRY/BLOCK discriminator. A step
# can OPT IN to scored self-evaluation (``self_evaluate=True`` with a min score
# and a retry cap) for a finer quality gradient. This path is deliberately a
# SEPARATE function/result type so the existing verdict contract is untouched.

SELF_EVAL_SYSTEM_PROMPT = """\
You are a strict reviewer scoring whether a subagent's result satisfies a task \
step's acceptance criteria. You receive the step's task, its criteria, the \
produced result, and verification evidence (test/lint/build status).

Score the result from 1 to 10:
- 9-10: fully satisfies the criteria, production-quality;
- 7-8: satisfies the criteria with only minor gaps;
- 4-6: partial work, meaningful gaps remain, fixable on retry;
- 1-3: unsalvageable or wrong.

Reply ONLY with one JSON object on one line:
{"score": <integer 1-10>, "reason": "<one sentence>", "feedback": "<actionable guidance if score is low>"}
"""


class SelfEvalResult(NamedTuple):
    """A scored self-evaluation. ``score is None`` means a judge outage."""

    score: int | None
    reason: str
    feedback: str


def _parse_self_eval_response(raw: str) -> SelfEvalResult:
    """Parse a scored review; a missing/invalid score is an outage (None)."""
    data = _load_json_object(raw)
    if data is not None:
        try:
            score = int(data.get("score"))
        except (TypeError, ValueError):
            score = -1
        if 1 <= score <= 10:
            return SelfEvalResult(
                score, str(data.get("reason") or ""), str(data.get("feedback") or "")
            )
    logger.warning("StepJudge self-eval: no usable score in response")
    return SelfEvalResult(None, "self-eval parse failure (no usable score)", "")


def _clip_for_judge(text: str, limit: int | None = None) -> str:
    """Clip a long result head+tail so its CONCLUSION survives the budget.

    A plain head slice hid exactly what many judge criteria ask for: the
    "dependency-ordered implementation plan" / final verdict lives at the END of
    a long report. The middle is dropped with a visible marker so the judge
    knows the text was clipped.
    """
    budget = int(limit if limit is not None else STEP_JUDGE["max_result_chars"])
    if budget <= 0 or len(text) <= budget:
        return text
    marker = "\n\n[... middle omitted for the judge ...]\n\n"
    head = (budget * 7) // 10
    tail = budget - head - len(marker)
    if tail <= 0:
        return text[:budget]
    return text[:head] + marker + text[-tail:]


async def judge_step_self_eval(
    step_task: str,
    criteria: str | None,
    result_text: str,
    evidence_summary: str | None = None,
    structured_result: dict | None = None,
) -> SelfEvalResult:
    """Score a step result 1-10 for the opt-in self-evaluation mode.

    Mirrors :func:`judge_step_result`'s LLM call and truncation so the two modes
    stay behaviourally aligned; an outage returns ``score=None`` (never a fake
    passing score), which the consumer treats like a judge UNKNOWN.
    """
    if structured_result is not None:
        judged_text = json.dumps(structured_result, ensure_ascii=False, default=str)
    else:
        judged_text = result_text or ""
    prompt = _build_judge_prompt(
        step_task,
        criteria,
        _clip_for_judge(judged_text),
        evidence_summary if STEP_JUDGE["evidence_aware"] else None,
    )
    try:
        llm = build_taskflow_llm(temperature=0)
        response = await llm.ainvoke(
            [
                {"role": "system", "content": SELF_EVAL_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
        )
        raw = response.content if hasattr(response, "content") else str(response)
        return _parse_self_eval_response(str(raw))
    except Exception as exc:  # noqa: BLE001 - the judge-outage boundary
        logger.warning("StepJudge self-eval failed (outage): {}", exc)
        return SelfEvalResult(None, f"self-eval error (outage): {exc}", "")


async def judge_step_result(
    step_task: str,
    criteria: str | None,
    result_text: str,
    evidence_summary: str | None = None,
    structured_result: dict | None = None,
) -> JudgeResult:
    """Judge whether a subagent's step result satisfies its validation criteria.

    ``structured_result`` (already validated against the step's
    ``response_schema`` by the caller) is judged instead of the truncated raw
    text when present: the judge then reasons over the actual values rather than
    re-parsing prose.

    A judge outage (model failure, unparseable output) returns ``UNKNOWN`` —
    never a fake PASS. See the module docstring for the consumer-side policy.
    """
    if structured_result is not None:
        judged_text = json.dumps(structured_result, ensure_ascii=False, default=str)
    else:
        judged_text = result_text or ""
    prompt = _build_judge_prompt(
        step_task,
        criteria,
        _clip_for_judge(judged_text),
        evidence_summary if STEP_JUDGE["evidence_aware"] else None,
    )
    try:
        llm = build_taskflow_llm(temperature=0)
        response = await llm.ainvoke(
            [
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
        )
        raw = response.content if hasattr(response, "content") else str(response)
        return _parse_judge_response(str(raw))
    except Exception as exc:  # noqa: BLE001 - the judge-outage boundary
        logger.warning("StepJudge failed, returning UNKNOWN: {}", exc)
        return JudgeResult(StepVerdict.UNKNOWN, f"judge error (outage): {exc}", "")
