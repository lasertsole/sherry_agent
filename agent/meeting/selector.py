"""The meeting selector: one structured verdict per turn (spike, plan §13.2).

The selector is a TOOL-LESS auxiliary call that reads the discussion history
and answers :class:`MeetingTurnDecision` — who speaks next and why, ordered by
the business dependencies the execution plan carries. ``next_speaker_id=None``
means the discussion converged.

The real path is ``build_auxiliary_llm(temperature=0, thinking=False)
.with_structured_output(MeetingTurnDecision)`` (same seam as
``summary_generation``'s ``SummaryDoc``). Tests inject a fake.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from loguru import logger

from agent.meeting.models import MeetingTurnDecision

__all__ = ["SelectorFn", "make_selector", "SELECTOR_SYSTEM_PROMPT"]

SELECTOR_SYSTEM_PROMPT = """You are a meeting scheduler. Read the discussion history and the
participants, then decide who speaks NEXT and why.

Rules:
- Order speakers by BUSINESS PREREQUISITES (frontend before backend, backend
  before the database), never round-robin or randomly.
- Participants respond to what was already said; do not repeat an opinion.
- When the discussion converged (the question is answered and every phase's
  completion criteria is met), return nextSpeakerId=null with the reason.
- The host is NOT a candidate speaker."""


class SelectorFn(Protocol):
    """The injectable selector seam: history + participants → one verdict."""

    def __call__(
        self, history: list[dict[str, Any]], participants: list[dict[str, Any]]
    ) -> MeetingTurnDecision: ...


def _history_payload(history: list[dict[str, Any]], participants: list[dict[str, Any]]) -> str:
    return json.dumps({"discussion": history, "participants": participants}, ensure_ascii=False)


def make_selector(llm: Any = None) -> SelectorFn:
    """The real selector bound to the auxiliary model's structured output."""

    def decide(
        history: list[dict[str, Any]], participants: list[dict[str, Any]]
    ) -> MeetingTurnDecision:
        if llm is None:
            from models.LLMs.auxiliary_llm.core import build_auxiliary_llm

            model = build_auxiliary_llm(temperature=0, thinking=False)
            structured = model.with_structured_output(MeetingTurnDecision)
        else:
            structured = llm
        messages = [
            {"role": "system", "content": SELECTOR_SYSTEM_PROMPT},
            {"role": "user", "content": _history_payload(history, participants)},
        ]
        try:
            return structured.invoke(messages)
        except Exception as exc:  # provider failures must not kill the meeting
            logger.warning("meeting selector invoke failed: {}", exc)
            return MeetingTurnDecision(reason=f"selector failed: {exc}")

    return decide
