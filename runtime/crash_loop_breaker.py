"""Crash loop breaker: boot lifecycle guard.

Records every process boot into ``SRC_DIR/data/boot_lifecycle.json`` and trips
when 3+ unclean boots occur within a 5-minute window, so an auto-restart
supervisor cannot resurrect a crash-looping backend forever.

Reference: openclaw gateway-boot-lifecycle.ts (unclean threshold 3, 5 min window).

Design notes (see docs/harness/loop-prevention/README.md + audit corrections):
- Storage lives under ``config.path.SRC_DIR / "data"`` (NOT ``Path.cwd()/data``).
- A corrupted/unreadable state file is treated as an empty (first-boot) state:
  the breaker must never block startup because of its own bookkeeping.
- The clean-exit marker (``mark_clean_exit`` / ``was_last_exit_clean``) lives in
  the same state file and is one-shot: ``record_boot`` consumes it, so a hard
  crash after a clean exit correctly reports the next boot as unclean.

This module is deliberately free of service wiring: Task 9 (``server/__main__.py``)
consumes ``record_boot`` / ``is_tripped`` / ``clear`` / ``mark_clean_exit`` /
``was_last_exit_clean`` at startup and via ``atexit``.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from loguru import logger

from config.path import SRC_DIR

# Unclean boots within this window (seconds) that trip the breaker: 5 minutes.
WINDOW_S = 300
# Number of unclean boots within the window that trip the breaker.
TRIP_THRESHOLD = 3
# Boot records older than this (seconds) are pruned on every record_boot.
RETENTION_S = 3600

# Module-level so tests can monkeypatch it into a tmp_path.
STATE_PATH = SRC_DIR / "data" / "boot_lifecycle.json"

_BOOTS_KEY = "boots"
_CLEAN_EXIT_KEY = "last_exit_clean"
_REASON_MAX_LEN = 200


def _empty_state() -> dict[str, Any]:
    return {_BOOTS_KEY: [], _CLEAN_EXIT_KEY: False}


def _ts_of(record: dict[str, Any]) -> float | None:
    """Extract a valid numeric ``ts`` from a boot record, else None."""
    try:
        ts = float(record["ts"])
    except (KeyError, TypeError, ValueError):
        return None
    return ts if ts == ts else None  # drop NaN too


def _read_state() -> dict[str, Any]:
    """Read the state file; missing file → first boot, corrupt → empty state."""
    if not STATE_PATH.exists():
        return _empty_state()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(
            "CrashLoopBreaker: state file corrupted/unreadable, treating as first boot: {} ({})",
            STATE_PATH,
            e,
        )
        return _empty_state()
    if not isinstance(data, dict):
        logger.warning(
            "CrashLoopBreaker: state file has unexpected shape, treating as first boot: {}",
            STATE_PATH,
        )
        return _empty_state()
    return data


def _write_state(state: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _try_write_state(state: dict[str, Any]) -> None:
    """Best-effort write: the breaker must never take the process down over its own bookkeeping."""
    try:
        _write_state(state)
    except OSError as e:
        logger.error("CrashLoopBreaker: failed to write state file {}: {}", STATE_PATH, e)


def record_boot(clean: bool, reason: str = "") -> bool:
    """Record one process boot; return the current ``is_tripped()`` verdict.

    Args:
        clean: True when the previous shutdown was a clean exit (marker seen),
            False after a crash / hard kill / unknown shutdown.
        reason: Short reason for the boot (truncated to 200 chars).

    The appended record ``{ts, clean, reason}`` is pruned against RETENTION_S.
    Also consumes the one-shot clean-exit marker.
    """
    now = time.time()
    state = _read_state()

    boots = state.get(_BOOTS_KEY)
    if not isinstance(boots, list):
        boots = []
    boots = [r for r in boots if (ts := _ts_of(r)) is not None and now - ts < RETENTION_S]
    boots.append({"ts": now, "clean": bool(clean), "reason": (reason or "")[:_REASON_MAX_LEN]})

    state[_BOOTS_KEY] = boots
    state[_CLEAN_EXIT_KEY] = False  # one-shot consumption (see module docstring)
    _try_write_state(state)

    return is_tripped()


def is_tripped() -> bool:
    """Read-only check: unclean boots within WINDOW_S >= TRIP_THRESHOLD."""
    now = time.time()
    boots = _read_state().get(_BOOTS_KEY)
    if not isinstance(boots, list):
        return False
    unclean_in_window = [
        r
        for r in boots
        if (ts := _ts_of(r)) is not None and now - ts < WINDOW_S and not r.get("clean")
    ]
    return len(unclean_in_window) >= TRIP_THRESHOLD


def clear() -> None:
    """Delete the state file (manual reset)."""
    try:
        STATE_PATH.unlink(missing_ok=True)
    except OSError as e:
        logger.error("CrashLoopBreaker: failed to clear state file {}: {}", STATE_PATH, e)


def mark_clean_exit() -> None:
    """Flag the current process as exiting cleanly (call from atexit — Task 9)."""
    state = _read_state()
    state[_CLEAN_EXIT_KEY] = True
    _try_write_state(state)


def was_last_exit_clean() -> bool:
    """True only if the previous shutdown ran ``mark_clean_exit()`` (default: False)."""
    return bool(_read_state().get(_CLEAN_EXIT_KEY, False))
