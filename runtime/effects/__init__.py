"""Restart-recovery effects: a durable intent log, an opt-in worker, and the
terminal-declaration table.

The package is additive: every existing restart path keeps running, and the
worker only starts when ``effects.enabled`` + ``effects.worker_enabled`` are on
(and shadow mode is off). ``effects.intent_log`` is import-safe (stdlib +
SQLite); the handlers deliberately import the server/agent modules lazily.
"""

from __future__ import annotations

from .config import (
    effects_enabled,
    effects_execution_enabled,
    effects_shadow_enabled,
    max_attempts,
    poll_interval_s,
    restart_replay,
    retry_initial_s,
    retry_max_s,
    shadow_mode,
    worker_enabled,
)
from .intent_log import (
    IntentRow,
    enqueue,
    fetch_pending,
    mark_abandoned,
    mark_done,
    mark_in_flight,
    mark_retry,
    replay_pending,
)
from .worker import EffectResult, EffectWorker, get_worker

__all__ = [
    "EffectResult",
    "EffectWorker",
    "IntentRow",
    "effects_enabled",
    "effects_execution_enabled",
    "effects_shadow_enabled",
    "enqueue",
    "fetch_pending",
    "get_worker",
    "mark_abandoned",
    "mark_done",
    "mark_in_flight",
    "mark_retry",
    "max_attempts",
    "poll_interval_s",
    "replay_pending",
    "restart_replay",
    "retry_initial_s",
    "retry_max_s",
    "shadow_mode",
    "worker_enabled",
]
