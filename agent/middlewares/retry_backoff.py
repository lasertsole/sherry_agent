"""Jittered exponential backoff and adaptive rate-limit backoff.

Equivalent to hermes-agent's ``agent/retry_utils.py``.

Provides:
- ``jittered_backoff()`` — decorrelated exponential backoff to prevent
  thundering-herd retry spikes.
- ``adaptive_rate_limit_backoff()`` — short exponential for first N attempts,
  then progressively wider waits for persistent rate-limiting.

This module is a utility library used by other middlewares (e.g.
:class:`RetryGuard`); it does **not** implement the ``AgentMiddleware``
interface itself.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass


@dataclass
class BackoffConfig:
    base_delay: float = 5.0
    max_delay: float = 120.0
    jitter_ratio: float = 0.5


@dataclass
class AdaptiveConfig:
    short_attempts: int = 3
    long_delays: tuple[float, ...] = (30.0, 60.0, 90.0, 120.0)


def jittered_backoff(
    attempt: int,
    config: BackoffConfig | None = None,
) -> float:
    """Compute a jittered exponential backoff delay.

    Formula: ``min(base * 2^(attempt-1), max_delay) + jitter``

    Parameters
    ----------
    attempt : int
        1-based attempt number.
    config : BackoffConfig | None
        Tuning knobs.  Defaults used if ``None``.

    Returns
    -------
    float
        Delay in seconds.
    """
    cfg = config or BackoffConfig()
    if attempt < 1:
        attempt = 1

    raw = min(cfg.base_delay * (2 ** (attempt - 1)), cfg.max_delay)
    jitter = raw * cfg.jitter_ratio * random.random()
    return raw + jitter


def adaptive_rate_limit_backoff(
    attempt: int,
    adaptive_config: AdaptiveConfig | None = None,
    backoff_config: BackoffConfig | None = None,
) -> float:
    """Adaptive backoff: short exponential for first N attempts, then wide waits.

    For the first ``short_attempts`` calls, uses normal exponential backoff.
    After that, uses progressively wider delays from ``long_delays``.

    Parameters
    ----------
    attempt : int
        1-based attempt number.
    adaptive_config : AdaptiveConfig | None
        Tuning knobs for the adaptive policy.
    backoff_config : BackoffConfig | None
        Tuning knobs for the short-phase exponential.

    Returns
    -------
    float
        Delay in seconds.
    """
    acfg = adaptive_config or AdaptiveConfig()
    bcfg = backoff_config or BackoffConfig()

    if attempt <= acfg.short_attempts:
        return jittered_backoff(attempt, bcfg)

    long_index = min(attempt - acfg.short_attempts - 1, len(acfg.long_delays) - 1)
    return acfg.long_delays[long_index]
