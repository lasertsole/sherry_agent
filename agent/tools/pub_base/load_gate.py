"""The SUBAGENT lane's environment gate: is the machine still able to take work?

The lane is COUNTING backpressure (``LANE_SYSTEM["subagent_max_concurrent"]``,
"how many"); this gate is the environment-aware half ("can the machine still
carry one more"). They are orthogonal and ordered: the gate runs BEFORE
``lane_slot(SUBAGENT)``, so a stopped dispatch never occupies a slot.

Two thresholds, and only one of them stops anything:

* ``>= *_warn`` (default 90%) — advisory. The lane cap already throttles here,
  so this only logs; adding a second gate at the same level would double up.
* ``>= *_stop`` (default 95%) — hard stop. A dispatch that would land here is
  deferred (not queued in the lane), and the run stays PENDING.

Criteria live HERE and nowhere else (thresholds come from ``LANE_SYSTEM``).

Two properties the callers rely on:

* **fail-open** — a psutil failure returns an ok verdict and logs. Dispatch must
  never stall because the monitor broke. (Only sampling fails open; a genuine
  ``stop`` from a working sample is honoured.)
* **zero event-loop blocking** — sampling costs seconds (three CPU intervals),
  so it runs in ``asyncio.to_thread`` and only once per TTL for the whole
  process: every waiting run reads the SAME snapshot, and the lock's
  double-check means N concurrent callers trigger exactly one sample.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from loguru import logger

from config.features import LANE_SYSTEM

__all__ = ["LoadGateVerdict", "check_load_gate", "get_load_gate", "reset_load_gate"]


@dataclass(frozen=True)
class LoadGateVerdict:
    """One load verdict. ``cpu`` / ``ram`` are percentages (-1 when unread)."""

    ok: bool
    stop: bool
    cpu: float
    ram: float


_OK = LoadGateVerdict(ok=True, stop=False, cpu=0.0, ram=0.0)

#: The process-wide shared snapshot. Starts OK on purpose (fail-open: nothing has
#: been observed yet, so nothing is refused).
_snapshot: LoadGateVerdict = _OK
_snapshot_at: float = 0.0
_snapshot_lock: asyncio.Lock | None = None


def _sample_cpu_mean(times: int, interval: float) -> float:
    """Mean of ``times`` CPU samples — one sample is too noisy to gate on.

    Blocking by construction (``psutil.cpu_percent(interval=…)`` sleeps); only
    ever call it from a worker thread.
    """
    import psutil

    samples = [psutil.cpu_percent(interval=interval) for _ in range(times)]
    return sum(samples) / len(samples) if samples else 0.0


def check_load_gate() -> LoadGateVerdict:
    """Sample the host and decide (blocking — for ``asyncio.to_thread``).

    Fail-open on any sampling error; a working sample is honoured verbatim.
    """
    if not LANE_SYSTEM["load_gate_enabled"]:
        return _OK
    try:
        import psutil

        cpu = _sample_cpu_mean(3, float(LANE_SYSTEM["load_gate_sample_interval_s"]))
        vm = psutil.virtual_memory()
        ram = ((vm.total - vm.available) / vm.total * 100.0) if vm.total else 0.0
    except Exception:  # noqa: BLE001 — a broken monitor must never block dispatch
        logger.warning("load gate: sampling failed, failing open")
        return LoadGateVerdict(ok=True, stop=False, cpu=-1.0, ram=-1.0)

    stop = cpu >= float(LANE_SYSTEM["load_gate_cpu_stop"]) or ram >= float(
        LANE_SYSTEM["load_gate_ram_stop"]
    )
    if not stop and (
        cpu >= float(LANE_SYSTEM["load_gate_cpu_warn"])
        or ram >= float(LANE_SYSTEM["load_gate_ram_warn"])
    ):
        logger.warning(
            "load gate: elevated load cpu={:.0f}% ram={:.0f}% (proceeding — the lane cap throttles here)",
            cpu,
            ram,
        )
    return LoadGateVerdict(ok=not stop, stop=stop, cpu=cpu, ram=ram)


def _lock() -> asyncio.Lock:
    """The snapshot lock, created lazily (module import must not need a loop)."""
    global _snapshot_lock
    if _snapshot_lock is None:
        _snapshot_lock = asyncio.Lock()
    return _snapshot_lock


async def get_load_gate() -> LoadGateVerdict:
    """The shared verdict (zero-blocking read; a stale snapshot refreshes once).

    Within the TTL every caller gets the cached verdict without touching psutil.
    Past it, one caller takes the lock and samples in a worker thread while the
    others wait and then read the fresh snapshot (double-checked).
    """
    global _snapshot, _snapshot_at
    ttl = float(LANE_SYSTEM["load_gate_snapshot_ttl_s"])
    if time.monotonic() - _snapshot_at < ttl:
        return _snapshot
    async with _lock():
        if time.monotonic() - _snapshot_at < ttl:
            return _snapshot  # another run refreshed it while we waited
        _snapshot = await asyncio.to_thread(check_load_gate)
        _snapshot_at = time.monotonic()
        return _snapshot


def reset_load_gate() -> None:
    """Drop the cached snapshot (tests; also after flipping the config)."""
    global _snapshot, _snapshot_at, _snapshot_lock
    _snapshot = _OK
    _snapshot_at = 0.0
    _snapshot_lock = None
