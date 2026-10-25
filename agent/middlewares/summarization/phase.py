"""Compact phase + reason classification (ZCode ``CompactPhase`` / ``CompactReason``).

These are DERIVED labels over the existing T1-T5 trigger strings — they never
change a trigger, a route or the recovery loop (R7). They exist so the
trajectory ledger and the logs can say WHEN in the turn lifecycle a compression
ran and WHY, mirroring ZCode's explicit taxonomy.
"""

from __future__ import annotations

from enum import StrEnum


class CompactPhase(StrEnum):
    """When a compression runs in the turn lifecycle."""

    STANDALONE_TURN = "standalone_turn"  # user-requested (compact_now / future /compact)
    PRE_REQUEST = "pre_request"  # T1 before_agent + T2 wrap_model_call pre-call
    MID_TURN = "mid_turn"  # a post-tool model call (sherry folds this into T2)
    REACTIVE = "reactive"  # T4/T5 provider-error forced recovery
    POST_RESPONSE = "post_response"  # T3 post-response re-check (sherry-specific)


class CompactReason(StrEnum):
    """Why a compression runs (coarse bucket; the trigger keeps the detail)."""

    CONTEXT_LIMIT = "context_limit"  # T1/T2/T3: estimate or reported usage
    PROVIDER_OVERFLOW = "provider_overflow"  # T4/T5: provider rejected the request
    USER_REQUESTED = "user_requested"  # the operator asked for it
    MODEL_DOWNSHIFT = "model_downshift"  # a session model profile with a smaller window


_TRIGGER_TO_PHASE_REASON: dict[str, tuple[CompactPhase, CompactReason]] = {
    "T1": (CompactPhase.PRE_REQUEST, CompactReason.CONTEXT_LIMIT),
    "T2": (CompactPhase.PRE_REQUEST, CompactReason.CONTEXT_LIMIT),
    "T3": (CompactPhase.POST_RESPONSE, CompactReason.CONTEXT_LIMIT),
    "T4": (CompactPhase.REACTIVE, CompactReason.PROVIDER_OVERFLOW),
    "T5": (CompactPhase.REACTIVE, CompactReason.PROVIDER_OVERFLOW),
    "USER": (CompactPhase.STANDALONE_TURN, CompactReason.USER_REQUESTED),
}


def derive_phase_reason(trigger: str) -> tuple[CompactPhase, CompactReason]:
    """Derive ``(phase, reason)`` from an existing trigger string (R7).

    Unknown triggers fall back to PRE_REQUEST/CONTEXT_LIMIT (the conservative
    bucket) so an event can never crash on a new trigger name.
    """
    return _TRIGGER_TO_PHASE_REASON.get(
        trigger, (CompactPhase.PRE_REQUEST, CompactReason.CONTEXT_LIMIT)
    )


__all__ = [
    "CompactPhase",
    "CompactReason",
    "derive_phase_reason",
]
