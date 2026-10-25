"""Per-provider sampling-time repetition penalty defaults.

These are the fallback values for the per-family `.models` overrides
(``*_FREQUENCY_PENALTY`` / ``*_PRESENCE_PENALTY``). A blank override keeps the
global value; an explicit ``0`` sends the provider's disabled value.

This is the SAMPLING-side counterpart of ``REPETITION_GUARD``: the guard detects
repetition after generation, these penalties make repetition less likely during
generation. The two are complementary, not alternatives.
"""

from typing import TypedDict


class RepetitionPenaltyConfig(TypedDict):
    """Sampling-time repetition penalty defaults."""

    #: OpenAI-compatible + Google ``frequency_penalty`` (-2.0..2.0, 0 = disabled).
    frequency_penalty: float
    #: OpenAI-compatible + Google ``presence_penalty`` (-2.0..2.0, 0 = disabled).
    presence_penalty: float
    #: Ollama native ``repeat_penalty`` (0.0..2.0, 1.0 = disabled).
    repeat_penalty: float


REPETITION_PENALTY: RepetitionPenaltyConfig = {
    # Light penalties: low enough to preserve reasoning/creativity, high enough
    # to reduce repeated spans. PI agent / openclaw commonly use 0.1-0.5.
    "frequency_penalty": 0.3,
    "presence_penalty": 0.2,
    # llama.cpp's typical native value (1.0 = disabled).
    "repeat_penalty": 1.1,
}
