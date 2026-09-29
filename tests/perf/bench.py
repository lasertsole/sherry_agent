"""Timing harness for the ``tests/perf`` suite.

Two ideas keep these tests useful on a slow, loaded box (a PROot container, a
shared CI runner) instead of flaky:

* **Ratios, not milliseconds.** A case measures the same code at two input sizes
  and asserts the growth factor. Load slows both measurements together, so the
  ratio survives what an absolute budget would not.
* **A loose absolute ceiling only as a catastrophe net** — each case may name
  one, sized an order of magnitude above the measured cost so it can only fire
  on a real algorithmic regression.

Every case prints its measurement, so a run doubles as a coarse profile:
``uv run --no-sync pytest tests/perf -q -s``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from statistics import median
from time import perf_counter
from typing import Any

# A 10x input is linear at ~10x cost; anything past this is superlinear enough
# to matter (a quadratic implementation lands near 100x).
MAX_GROWTH_FACTOR = 30.0


def timed(fn: Callable[[], Any], *, repeats: int = 5, warmup: int = 1) -> float:
    """Median wall time of ``fn()`` in seconds, after a warmup pass.

    The median (not the mean) keeps one scheduler hiccup from deciding a ratio.
    """
    for _ in range(warmup):
        fn()
    samples: list[float] = []
    for _ in range(repeats):
        start = perf_counter()
        fn()
        samples.append(perf_counter() - start)
    return median(samples)


async def timed_async(
    fn: Callable[[], Awaitable[Any]], *, repeats: int = 5, warmup: int = 1
) -> float:
    """Awaiting twin of :func:`timed`, for coroutine-shaped hot paths."""
    for _ in range(warmup):
        await fn()
    samples: list[float] = []
    for _ in range(repeats):
        start = perf_counter()
        await fn()
        samples.append(perf_counter() - start)
    return median(samples)


def scaled(
    case: str,
    measure: Callable[[int], Any],
    *,
    prepare: Callable[[int], None] | None = None,
    big_multiplier: int = 10,
    ceiling_seconds: float | None = None,
    max_factor: float = MAX_GROWTH_FACTOR,
    **timing: Any,
) -> tuple[float, float]:
    """Assert that ``measure(10x)`` costs less than ``max_factor`` times ``measure(1x)``.

    ``prepare(scale)`` builds the world for a scale and is NOT timed; ``measure``
    is. Returns ``(small, big)`` seconds so a caller can report them.
    """
    times: dict[int, float] = {}
    for scale in (1, big_multiplier):
        if prepare is not None:
            prepare(scale)
        times[scale] = timed(lambda s=scale: measure(s), **timing)

    return _report(case, times, big_multiplier, max_factor, ceiling_seconds)


def _report(
    case: str,
    times: dict[int, float],
    big_multiplier: int,
    max_factor: float,
    ceiling_seconds: float | None,
) -> tuple[float, float]:
    """Shared verdict: print the measurement and enforce the growth budget."""
    small, big = times[1], times[big_multiplier]
    factor = big / small if small else float("inf")
    print(
        f"[perf] {case}: {big_multiplier}x input → {small * 1000:.3f}ms → "
        f"{big * 1000:.3f}ms (×{factor:.1f}, budget ×{max_factor:g})"
    )
    assert factor < max_factor, (
        f"{case}: {big_multiplier}x the input cost ×{factor:.1f} the time "
        f"({small * 1000:.3f}ms → {big * 1000:.3f}ms) — superlinear growth"
    )
    if ceiling_seconds is not None:
        assert big < ceiling_seconds, (
            f"{case}: the large case took {big * 1000:.0f}ms, past the "
            f"{ceiling_seconds * 1000:.0f}ms catastrophe ceiling"
        )
    return small, big
