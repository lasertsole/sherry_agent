"""Subagent result-text caps that scale with the serving model's output budget.

The original caps were calibrated against an 8000-token output window. A user
who sets ``*_MAX_OUTPUT_TOKEN`` gets the same shape scaled linearly from that
calibration (an 8192 setting is the ×1.024 micro-adjusted baseline); leaving the
setting empty returns the legacy values unchanged.

Only the SPAWNING side resolves the model; the resulting limit is frozen on the
run record so completion, delivery, announcement and transcript reads all use
the same value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config.features import SUBAGENT_INFRA, TOOLS_TIMEOUTS
from pub.func.message.text_limits import MAX_INLINE_TEXT_CHARS

__all__ = [
    "LEGACY_OUTPUT_WINDOW",
    "SubagentOutputCaps",
    "resolve_child_max_output_tokens",
    "resolve_subagent_output_caps",
]

#: The output window the original hardcoded caps were calibrated against.
LEGACY_OUTPUT_WINDOW = 8000

#: Original values (kept here as the "unset" / legacy path).
_LEGACY_STORED_BYTES = int(SUBAGENT_INFRA["helpers_frozen_result_cap_bytes"])
_LEGACY_DELIVERY_CHARS = 4000
_LEGACY_SUMMARY_CHARS = 2000
_LEGACY_JUDGE_CHARS = int(MAX_INLINE_TEXT_CHARS)
_LEGACY_TRANSCRIPT_TOKENS = int(TOOLS_TIMEOUTS["session_messages_token_budget"])
_LEGACY_PER_MESSAGE_TOKENS = int(TOOLS_TIMEOUTS["session_messages_per_message_token_budget"])


@dataclass(frozen=True)
class SubagentOutputCaps:
    """The six result-text limits derived in one place."""

    stored_bytes: int
    delivery_chars: int
    summary_chars: int
    judge_chars: int
    transcript_tokens: int
    per_message_tokens: int


def resolve_subagent_output_caps(max_output_tokens: int | None) -> SubagentOutputCaps:
    """Return the caps for a child's serving model.

    ``None`` (the user left the model's max-output setting empty) keeps the
    legacy values byte-for-byte. A positive value scales every cap linearly at
    ``legacy * value / 8000``; 8192 therefore yields the 1.024× micro-adjusted
    8192 baseline.
    """

    def scaled(legacy: int) -> int:
        if not isinstance(max_output_tokens, int) or max_output_tokens <= 0:
            return legacy
        return max(1, round(legacy * max_output_tokens / LEGACY_OUTPUT_WINDOW))

    return SubagentOutputCaps(
        stored_bytes=scaled(_LEGACY_STORED_BYTES),
        delivery_chars=scaled(_LEGACY_DELIVERY_CHARS),
        summary_chars=scaled(_LEGACY_SUMMARY_CHARS),
        judge_chars=scaled(_LEGACY_JUDGE_CHARS),
        transcript_tokens=scaled(_LEGACY_TRANSCRIPT_TOKENS),
        per_message_tokens=scaled(_LEGACY_PER_MESSAGE_TOKENS),
    )


def resolve_child_max_output_tokens(
    *,
    model_profile: dict[str, Any] | None,
    model_override: str | None,
    functional_role: Any,
    model_tier: str | None,
    role: Any,
) -> int | None:
    """Resolve the output budget of the model the child will actually run on.

    Mirrors ``spawn/core.py::_build_child_llm`` precedence:

    1. a per-role model profile (built via ``build_main_llm_for_profile``, which
       uses the MAIN family's output cap);
    2. a named ``model_override`` — its budget is unknown, so ``None``;
    3. a specialized role's ``model_tier`` (main / auxiliary);
    4. the depth role: ORCHESTRATOR → main, LEAF → auxiliary.

    A missing/blank setting returns ``None`` (legacy caps). Invalid values are
    treated the same way: the guard already blocks them at boot, and a failed
    telemetry read must not break a spawn.
    """
    from models.LLMs.provider_max_output import configured_max_output

    from .types.capability import SubagentSessionRole
    from .types.functional_role import FunctionalRole

    if model_profile and model_profile.get("model"):
        key = "MAIN_LLM_MAX_OUTPUT_TOKEN"
    elif model_override:
        return None
    elif functional_role != FunctionalRole.GENERAL and model_tier == "main":
        key = "MAIN_LLM_MAX_OUTPUT_TOKEN"
    elif functional_role != FunctionalRole.GENERAL and model_tier == "auxiliary":
        key = "AUXILIARY_LLM_MAX_OUTPUT_TOKEN"
    elif role == SubagentSessionRole.ORCHESTRATOR:
        key = "MAIN_LLM_MAX_OUTPUT_TOKEN"
    else:
        key = "AUXILIARY_LLM_MAX_OUTPUT_TOKEN"

    try:
        return configured_max_output(key)
    except ValueError:
        return None
