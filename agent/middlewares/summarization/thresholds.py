"""The single source of truth for the context-compression line.

The line is an ABSOLUTE token count, not a ratio. When a main-model output cap
is configured the provider's window is shared by input + output, so compaction
starts at ``window - max_output`` (the full input budget). With no cap the
legacy 80% line is the fallback.
"""

from __future__ import annotations

from config.features import SUMMARIZATION

__all__ = ["resolve_compression_threshold", "resolve_truncate_threshold"]


def resolve_compression_threshold(
    window: int,
    max_output: int | None,
    *,
    fallback_ratio: float | None = None,
) -> tuple[int, float]:
    """Return ``(threshold_tokens, compress_ratio)`` for one context window.

    ``max_output`` unset/zero/negative keeps the legacy
    ``window * compression_trigger_ratio`` line. A set cap uses
    ``max(window - max_output, 0)``. The ratio is always ``threshold / window``
    and is what the client displays.
    """
    ratio = (
        float(fallback_ratio)
        if fallback_ratio is not None
        else float(SUMMARIZATION["compression_trigger_ratio"])
    )
    try:
        window = int(window or 0)
    except (TypeError, ValueError):
        window = 0
    if window <= 0:
        return 0, 0.0

    try:
        output = int(max_output or 0)
    except (TypeError, ValueError):
        output = 0

    if output > 0:
        threshold = max(window - output, 0)
    else:
        threshold = int(window * ratio)
    return threshold, min(max(threshold / window, 0.0), 1.0)


def resolve_truncate_threshold(
    compression_threshold: int,
    *,
    fallback_ratio: float | None = None,
    truncate_ratio: float | None = None,
) -> int:
    """The softer preemptive-truncate line, proportionally below the hard line.

    Keeps the historical 0.70/0.80 relationship: when no output cap is set this
    is exactly ``window * 0.70``; with a cap it is ``0.875 * (window - cap)``.
    """
    fallback = (
        float(fallback_ratio)
        if fallback_ratio is not None
        else float(SUMMARIZATION["compression_trigger_ratio"])
    )
    soft = (
        float(truncate_ratio)
        if truncate_ratio is not None
        else float(SUMMARIZATION["preemptive_truncate_ratio"])
    )
    if compression_threshold <= 0:
        return 0
    scaled = round(compression_threshold * (soft / fallback)) if fallback > 0 else 0
    return max(1, min(int(compression_threshold), scaled))
