"""Output-repetition detection thresholds (cross-call and internal)."""

from typing import TypedDict


class RepetitionGuardConfig(TypedDict):
    """Output-repetition detection thresholds (cross-call and internal)."""

    min_content_length: int
    min_crosscall_length: int
    max_identical_outputs: int
    warn_after: int
    internal_repeat_ratio: float
    internal_min_lines: int
    char_run_min: int
    tail_chars: int
    phrase_min_repeats: int
    phrase_max_phrase_len: int
    max_history: int


REPETITION_GUARD: RepetitionGuardConfig = {
    "min_content_length": 20,
    "min_crosscall_length": 1,
    "max_identical_outputs": 3,
    "warn_after": 2,
    "internal_repeat_ratio": 0.6,
    "internal_min_lines": 6,
    "char_run_min": 8,
    "tail_chars": 500,
    "phrase_min_repeats": 5,
    "phrase_max_phrase_len": 10,
    "max_history": 30,
}
