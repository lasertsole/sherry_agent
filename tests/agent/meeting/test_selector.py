"""The selector seam: real schema + the injected-LLM path (plan §13.2).

The REAL selector binds ``build_auxiliary_llm().with_structured_output`` —
not invoked here (no model key in the unit gate); what is pinned is the
schema the model must satisfy and the fake-LLM wiring the tests reuse.
"""

from __future__ import annotations

import pytest

from agent.meeting.models import MeetingTurnDecision
from agent.meeting.selector import SELECTOR_SYSTEM_PROMPT, make_selector

pytestmark = [pytest.mark.unit]


class _FakeStructured:
    """Stands in for ``llm.with_structured_output(schema)``'s return value."""

    def __init__(self, owner: _FakeLLM) -> None:
        self.owner = owner

    def invoke(self, messages: list[dict]) -> MeetingTurnDecision:
        self.owner.seen = messages
        return self.owner.verdict


class _FakeLLM:
    """The seam make_selector(llm=...) consumes: the STRUCTURED caller itself."""

    def __init__(self, verdict: MeetingTurnDecision) -> None:
        self.verdict = verdict
        self.structured_calls = 0
        self.seen: list[dict] = []

    def invoke(self, messages: list[dict]) -> MeetingTurnDecision:
        self.structured_calls += 1
        return _FakeStructured(self).invoke(messages)


def test_the_real_selector_uses_the_injected_llm():
    verdict = MeetingTurnDecision(
        next_speaker_id="fe",
        reason="frontend is the prerequisite",
        execution_plan=[{"id": "p1", "allowedAgentIds": ["fe"], "prerequisites": []}],
    )
    fake = _FakeLLM(verdict)

    decision = make_selector(llm=fake)(
        [{"speaker_name": "Host", "content": "open"}],
        [{"agent_id": "fe", "name": "Frontend"}],
    )

    assert fake.structured_calls == 1
    assert decision.next_speaker_id == "fe"
    assert decision.execution_plan[0].id == "p1"
    system_message = decision and fake.seen[0]
    assert "meeting scheduler" in str(system_message["content"]).lower()
    user_payload = str(fake.seen[1]["content"])
    assert "Frontend" in user_payload and "open" in user_payload


def test_the_selector_prompt_forbids_round_robin_and_host_speaking():
    prompt = SELECTOR_SYSTEM_PROMPT.lower()
    assert "never round-robin" in prompt
    assert "not a candidate" in prompt
    assert "nextspeakerid=null" in prompt.replace(" ", "")
