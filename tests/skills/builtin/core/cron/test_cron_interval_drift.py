"""TDD tests for audit issue #22 — cron "every" interval drift.

Audit finding being pinned: ``_compute_next_run`` recomputed an ``every``
job's next run from ``now`` instead of the job's scheduled slot, so every
cycle accumulated the job's own duration into the period — a 60s job that
takes 30s actually fired every 90s and drifted further out forever.

Contract after the fix (fixed-phase grid):

1. After a run, the next slot is ``consumed_slot + k * every_ms`` with the
   smallest ``k`` landing strictly after now — the grid phase survives any
   job duration (no drift) and missed slots are skipped, never burst.
2. The first schedule (no consumed slot) starts the grid at ``now``.
3. A manual ``run_job`` consumes the job's next scheduled slot — the grid
   stays intact, no double-fire.
4. A service restart preserves still-future persisted slots for ``every``
   jobs; missing/past slots re-anchor from now.
"""

import asyncio
from types import SimpleNamespace

import pytest

from skills.builtin.core.cron.scripts import base as cron_base
from skills.builtin.core.cron.scripts.types import CronJob, CronJobState, CronPayload, CronSchedule

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

T0 = 1_700_000_000_000  # arbitrary fixed epoch ms
EVERY_MS = 60_000


class _FakeClock:
    """Controllable _now_ms replacement; on_job advances it to simulate duration."""

    def __init__(self, start_ms: int) -> None:
        self.now_ms = start_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, ms: int) -> None:
        self.now_ms += ms


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Fresh CronService on a tmp store with an injectable clock."""
    clock = _FakeClock(T0)
    monkeypatch.setattr(cron_base, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(cron_base, "_now_ms", clock)

    svc = cron_base.CronService()
    svc.store_path = tmp_path / "cron_jobs.json"

    async def fake_on_job(job: CronJob) -> None:
        clock.advance(30_000)  # every run "takes" 30s

    svc.set_on_job(fake_on_job)
    return SimpleNamespace(svc=svc, clock=clock)


def _every_job(slot_ms: int | None, job_id: str = "job00001") -> CronJob:
    return CronJob(
        id=job_id,
        name="interval-drift",
        enabled=True,
        schedule=CronSchedule(kind="every", every_ms=EVERY_MS),
        payload=CronPayload(message="tick"),
        state=CronJobState(next_run_at_ms=slot_ms),
    )


def _load_single_job(svc: cron_base.CronService, job: CronJob) -> CronJob:
    svc._load_store()
    svc._store.jobs.append(job)
    svc._save_store()
    return job


# ---------------------------------------------------------------------------
# Pure function: _compute_next_run anchoring
# ---------------------------------------------------------------------------


class TestComputeNextRunAnchoring:
    def test_anchor_prevents_duration_drift(self):
        """Overran slot: next must stay on the grid, not slide by the overrun."""
        schedule = CronSchedule(kind="every", every_ms=EVERY_MS)
        consumed_slot = T0
        now = T0 + 30_000  # job finished 30s late

        nxt = cron_base._compute_next_run(schedule, now, anchor_ms=consumed_slot)

        assert nxt == T0 + EVERY_MS, (
            f"expected the grid point T0+60s, got {nxt} "
            "(anchoring to `now` would push it to T0+90s and drift forever)"
        )

    def test_anchor_skips_missed_slots_without_burst(self):
        """Slot missed by 2.5 intervals: next is the FIRST grid point after now."""
        schedule = CronSchedule(kind="every", every_ms=EVERY_MS)
        now = T0 + 150_000

        nxt = cron_base._compute_next_run(schedule, now, anchor_ms=T0)

        assert nxt == T0 + 180_000

    def test_no_anchor_starts_grid_at_now(self):
        """First schedule (no consumed slot) keeps the current behavior."""
        schedule = CronSchedule(kind="every", every_ms=EVERY_MS)

        nxt = cron_base._compute_next_run(schedule, T0)

        assert nxt == T0 + EVERY_MS

    def test_at_kind_ignores_anchor(self):
        schedule = CronSchedule(kind="at", at_ms=T0 + 5_000)

        assert cron_base._compute_next_run(schedule, T0, anchor_ms=T0) == T0 + 5_000


# ---------------------------------------------------------------------------
# Service-level: the audit's drift scenario, three consecutive cycles
# ---------------------------------------------------------------------------


class TestExecuteJobNoDrift:
    def test_three_cycles_keep_fixed_grid(self, env):
        """A 30s-duration job on a 60s interval must fire at T60/T120/T180.

        Old code recomputed from `now` after each run: T90/T150/T210 — the
        period silently became 90s and drifted +30s per cycle.
        """
        svc, clock = env.svc, env.clock
        job = _load_single_job(svc, _every_job(slot_ms=T0))

        for cycle in range(1, 4):
            due = T0 + (cycle - 1) * EVERY_MS
            assert job.state.next_run_at_ms == due, (
                f"cycle {cycle}: schedule drifted to {job.state.next_run_at_ms}"
            )
            clock.now_ms = due  # scheduler fires the job at its slot
            asyncio.run(svc._execute_job(job))  # on_job advances the clock 30s
            assert job.state.next_run_at_ms == T0 + cycle * EVERY_MS

        # After 3 cycles of 30s durations the grid is exactly where it started.
        assert job.state.next_run_at_ms == T0 + 3 * EVERY_MS
        assert clock.now_ms == T0 + 2 * EVERY_MS + 30_000  # last slot + duration

    def test_manual_run_consumes_current_slot(self, env):
        """run_job mid-cycle pre-fires the pending slot; grid stays intact."""
        svc, clock = env.svc, env.clock
        job = _load_single_job(svc, _every_job(slot_ms=T0 + EVERY_MS))
        clock.advance(20_000)  # manual run at T0+20s, slot T0+60s pending

        ran = asyncio.run(svc.run_job(job.id, force=True))

        assert ran is True
        # run_job reloads the store from disk — read the reloaded object.
        reloaded = svc.get_job(job.id)
        assert reloaded is not None
        assert reloaded.state.next_run_at_ms == T0 + 2 * EVERY_MS, (
            "manual run must consume the pending slot, not reset the grid from now"
        )


# ---------------------------------------------------------------------------
# Restart semantics: _recompute_next_runs
# ---------------------------------------------------------------------------


class TestRecomputeNextRuns:
    def test_future_slot_survives_restart(self, env):
        svc = env.svc
        job = _load_single_job(svc, _every_job(slot_ms=T0 + 30_000))

        svc._recompute_next_runs()

        assert job.state.next_run_at_ms == T0 + 30_000, (
            "restart must not re-anchor a still-future persisted slot (drift)"
        )

    def test_past_slot_reanchors_from_now(self, env):
        svc, clock = env.svc, env.clock
        clock.advance(120_000)  # service down for 2 minutes
        job = _load_single_job(svc, _every_job(slot_ms=T0 + 60_000))

        svc._recompute_next_runs()

        assert job.state.next_run_at_ms == clock.now_ms + EVERY_MS

    def test_missing_slot_reanchors_from_now(self, env):
        svc = env.svc
        job = _load_single_job(svc, _every_job(slot_ms=None))

        svc._recompute_next_runs()

        assert job.state.next_run_at_ms == T0 + EVERY_MS
