"""CompactPhase/CompactReason derivation from the existing T1-T5 triggers (R7)."""

from __future__ import annotations

import pytest

from agent.middlewares.summarization.phase import (
    CompactPhase,
    CompactReason,
    derive_phase_reason,
)

pytestmark = [pytest.mark.unit]


@pytest.mark.parametrize(
    ("trigger", "phase", "reason"),
    [
        ("T1", CompactPhase.PRE_REQUEST, CompactReason.CONTEXT_LIMIT),
        ("T2", CompactPhase.PRE_REQUEST, CompactReason.CONTEXT_LIMIT),
        ("T3", CompactPhase.POST_RESPONSE, CompactReason.CONTEXT_LIMIT),
        ("T4", CompactPhase.REACTIVE, CompactReason.PROVIDER_OVERFLOW),
        ("T5", CompactPhase.REACTIVE, CompactReason.PROVIDER_OVERFLOW),
        ("USER", CompactPhase.STANDALONE_TURN, CompactReason.USER_REQUESTED),
    ],
)
def test_every_trigger_derives_the_expected_phase_and_reason(trigger, phase, reason):
    assert derive_phase_reason(trigger) == (phase, reason)


def test_unknown_trigger_falls_back_to_the_conservative_bucket():
    assert derive_phase_reason("T99") == (
        CompactPhase.PRE_REQUEST,
        CompactReason.CONTEXT_LIMIT,
    )


def test_enum_values_are_the_wire_strings():
    assert CompactPhase.REACTIVE == "reactive"
    assert CompactPhase.STANDALONE_TURN == "standalone_turn"
    assert CompactReason.PROVIDER_OVERFLOW == "provider_overflow"
    assert CompactReason.USER_REQUESTED == "user_requested"
