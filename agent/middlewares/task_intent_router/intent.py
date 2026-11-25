"""Rules-only classifier for a user message that arrives while a TaskFlow is active.

Why rules-only
--------------
The router runs on every inbound turn of a long task. Calling an LLM here would
add latency and cost to EVERY interruption, and a model misclassification could
cancel a running flow. The existing ``task_intent`` middleware established the
four-language, keyword-first, fail-open pattern; this classifier follows it.

Fail-open contract
------------------
Ambiguous input classifies as ``CONTINUE`` (treated as a normal continuation of
the current flow) so no message is ever dropped or misrouted into a destructive
action. Only an explicit cancellation phrase can produce ``CANCEL``.

The classes map to the WorkMode router intents:

* ``CANCEL``       — user wants the running task stopped  → cancel active flows
* ``SOFT_PREEMPT`` — user is steering / changing direction → queue for next turn
* ``RESUME``       — an answer to a pending HITL pause      → existing HITL path
* ``NEW``          — an unrelated fresh request            → queue as new task
* ``CONTINUE``     — more info for the current flow (default) → normal injection

Priority (highest first): CANCEL > SOFT_PREEMPT > RESUME > CONTINUE > NEW.
``CONTINUE`` is the fail-open default; ``NEW`` is only emitted when an explicit
"new task" phrase is present, otherwise the input is a continuation.
"""

from __future__ import annotations

import re
from enum import StrEnum

__all__ = ["TurnIntent", "classify_turn_intent"]


class TurnIntent(StrEnum):
    """The five router intents (see module docstring)."""

    CANCEL = "cancel"
    SOFT_PREEMPT = "soft_preempt"
    RESUME = "resume"
    CONTINUE = "continue"
    NEW = "new"


#: Explicit "stop the task" phrases, four languages. Kept tight: an accidental
#: match here cancels a flow, so only imperative/standalone forms qualify.
_CANCEL_PHRASES: tuple[str, ...] = (
    # English
    "stop",
    "cancel",
    "abort",
    "don't do",
    "do not do",
    "never mind",
    "forget it",
    "call it off",
    # Chinese
    "停下",
    "停一下",
    "停止",
    "取消",
    "别做了",
    "不要做了",
    "不用做了",
    "中止",
    "算了不",
    # Japanese
    "やめて",
    "止めて",
    "中止",
    "キャンセル",
    "取り消し",
    "もういい",
    # Korean
    "취소",
    "중단",
    "그만",
    "멈춰",
    "하지 마",
)

#: Standalone cancel commands (whole short message equals one of these). These
#: fire even where the word also appears in longer sentences only if the whole
#: message is short; a long sentence containing "stop" is probably not a cancel.
_CANCEL_EXACT: frozenset[str] = frozenset(
    {
        "stop",
        "cancel",
        "abort",
        "停下",
        "停一下",
        "停止",
        "取消",
        "别做了",
        "不要做了",
        "中止",
        "やめて",
        "止めて",
        "中止して",
        "キャンセル",
        "취소",
        "중단",
        "그만",
        "멈춰",
    }
)

#: "Change direction / wait, actually..." steering phrases.
_SOFT_PREEMPT_PHRASES: tuple[str, ...] = (
    "wait",
    "actually",
    "instead",
    "on second thought",
    "change of plan",
    "let's not",
    "hold on",
    "等等",
    "先别",
    "换个",
    "改一下",
    "改成",
    "方向不对",
    "其实",
    "不对",
    "ちょっと待",
    "待って",
    "やっぱり",
    "でも",
    "方向が違",
    "잠깐",
    "그런데",
    "대신",
    "아니",
    "방향이",
)

#: Explicit "start something new" phrases. Absence does NOT mean new — the
#: default stays CONTINUE (fail-open).
_NEW_PHRASES: tuple[str, ...] = (
    "new task",
    "start over",
    "different task",
    "新任务",
    "另一个任务",
    "重新开始",
    "换个任务",
    "新しいタスク",
    "別の作業",
    "やり直し",
    "새 작업",
    "다른 작업",
    "처음부터",
)

#: HITL resume cues (an answer to a pending approval/pause). The actual resume
#: is owned by the existing HITL path; recognizing it only avoids misrouting.
_RESUME_PATTERNS: tuple[str, ...] = (
    r"^(yes|no|ok|approve|allow|deny|reject|continue|go ahead)\b",
    r"^(是的|不是|好的|同意|允许|拒绝|批准|继续|可以|不行)",
    r"^(はい|いいえ|承認|許可|拒否|続けて|続行)",
    r"^(네|아니요|승인|허용|거부|계속)",
)

_SHORT_MESSAGE_CHARS = 80

# Standalone cancel commands may carry trailing punctuation / whitespace.
_CANCEL_EXACT_NORMALIZED = frozenset(
    re.sub(r"[\s!！.。,，~～]+$", "", phrase) for phrase in _CANCEL_EXACT
)


def _normalized(text: str) -> str:
    """Lowercase + trim trailing punctuation for exact-command matching."""
    return re.sub(r"[\s!！.。,，~～]+$", "", text.strip().lower())


def classify_turn_intent(content: str, *, has_pending_hitl: bool = False) -> TurnIntent:
    """Classify *content* into one of the five router intents.

    ``has_pending_hitl`` enables the ``RESUME`` branch: a short yes/no-style
    answer while an approval is pending is a resume, not a cancel/steer. Callers
    that do not track HITL state leave it False.

    The classifier never raises on ordinary input; a blank message is CONTINUE.
    """
    text = (content or "").strip().lower()
    if not text:
        return TurnIntent.CONTINUE

    # 1) A short standalone cancel command is unambiguous.
    if _normalized(text) in _CANCEL_EXACT_NORMALIZED:
        return TurnIntent.CANCEL

    # 2) An explicit cancel phrase anywhere (imperative forms). Require the
    #    phrase near the start for short messages to reduce false cancels.
    short = len(text) <= _SHORT_MESSAGE_CHARS
    if short:
        for phrase in _CANCEL_PHRASES:
            if text.startswith(phrase) or f" {phrase}" in text:
                return TurnIntent.CANCEL

    # 3) While an approval is pending, a short affirmative/negative answer is
    #    a HITL resume (the HITL layer owns the actual action).
    if has_pending_hitl and short:
        for pattern in _RESUME_PATTERNS:
            if re.match(pattern, text):
                return TurnIntent.RESUME

    # 4) Steering / change-of-direction.
    for phrase in _SOFT_PREEMPT_PHRASES:
        if text.startswith(phrase) or phrase in text:
            return TurnIntent.SOFT_PREEMPT

    # 5) An explicit fresh-task phrase. Otherwise fail open as CONTINUE.
    for phrase in _NEW_PHRASES:
        if phrase in text:
            return TurnIntent.NEW

    return TurnIntent.CONTINUE
