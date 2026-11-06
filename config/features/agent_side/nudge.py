"""Experience-extraction nudge settings.

The memory review is dispatched on every compression, plan extraction fires when
the plan completes at a turn boundary, and MAIN-session deletion schedules a
detached two-wave memory salvage before the purge.
"""

from typing import TypedDict


class NudgeConfig(TypedDict):
    """Compression-time nudge settings."""

    plan_extraction_enabled: bool
    #: The plan-completion pass is now a skill-quality nudge: it updates skills
    #: from success/failure evidence and never writes knowledge. Kept separate
    #: from ``plan_extraction_enabled`` so an upgrade can disable only the new
    #: LLM judge/retry loop without turning off the completion detector.
    skill_nudge_enabled: bool
    skill_nudge_max_attempts: int
    skill_nudge_quality_threshold: int
    #: Bounded user-correction / loaded-skill context (no full transcript).
    skill_nudge_feedback_turns: int
    skill_nudge_feedback_max_items: int
    skill_nudge_feedback_chars: int
    skill_nudge_loaded_skills_max: int
    #: After an exhausted judge/retry pass, do not hot-loop on the same plan
    #: snapshot for this long. A changed plan (new snapshot hash) retries at once.
    skill_nudge_failure_cooldown_s: int
    #: How the memory review builds its input.
    #:
    #: - ``cache_warm`` (default): the discarded messages VERBATIM, on the main
    #:   model the turn just ran on. The compaction cut is a prefix cut, so that
    #:   slice is exactly the request prefix the main model just served — the
    #:   provider answers it from its prefix cache and the review reads the real
    #:   text. Costs cache-read prices for the prefix plus one fresh instruction.
    #: - ``digest``: the folded view (``summarization/digest.py``). For a
    #:   deployment whose provider caches prefixes poorly (or whose review model
    #:   has a small window), the fold bounds the input regardless of provider
    #:   behaviour, at the price of losing the older detail.
    memory_review_mode: str
    #: Main-session deletion runs a detached, two-wave memory review over the
    #: captured transcript before the stores are purged. The delete request never
    #: waits for it; a process exit may abandon the unfinished review.
    session_end_review_enabled: bool
    #: Per-wave model-call budget for the session-end review. A timed-out wave is
    #: logged and abandoned; the deletion already completed.
    session_end_review_timeout_s: int


NUDGE: NudgeConfig = {
    "plan_extraction_enabled": True,
    "skill_nudge_enabled": True,
    "skill_nudge_max_attempts": 2,
    "skill_nudge_quality_threshold": 6,
    "skill_nudge_feedback_turns": 12,
    "skill_nudge_feedback_max_items": 8,
    "skill_nudge_feedback_chars": 2000,
    "skill_nudge_loaded_skills_max": 20,
    "skill_nudge_failure_cooldown_s": 3600,
    "memory_review_mode": "cache_warm",
    "session_end_review_enabled": True,
    "session_end_review_timeout_s": 120,
}
