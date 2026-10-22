"""Cron execution claim: one in-process lock for tick and manual runs.

The cron tick runs on the cron loop and ``run_job`` on the server loop. These
tests pin the trimmed design: in-memory claim + pre-advance under one lock, with
no persisted running flag and no second advance after the run.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from skills.builtin.core.cron.scripts import base as cron_base
from skills.builtin.core.cron.scripts.types import (
    CronJob,
    CronJobState,
    CronPayload,
    CronSchedule,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

T0 = 1_700_000_000_000
EVERY_MS = 60_000


class _Clock:
    def __init__(self, now: int = T0) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


@pytest.fixture()
def env(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(cron_base, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(cron_base, "_now_ms", clock)
    svc = cron_base.CronService()
    svc.store_path = tmp_path / "cron_jobs.json"
    calls: list[str] = []

    async def on_job(job: CronJob) -> None:
        calls.append(job.id)

    svc.set_on_job(on_job)
    return SimpleNamespace(svc=svc, clock=clock, calls=calls)


def _seed(svc: cron_base.CronService, job: CronJob) -> CronJob:
    svc._load_store()
    svc._store.jobs.append(job)
    svc._save_store()
    return job


def _job() -> CronJob:
    return CronJob(
        id="job-dedup",
        name="dedup",
        enabled=True,
        schedule=CronSchedule(kind="every", every_ms=EVERY_MS),
        payload=CronPayload(message="tick"),
        state=CronJobState(next_run_at_ms=T0),
    )


def test_second_execute_while_running_is_refused(env):
    svc, calls = env.svc, env.calls
    job = _seed(svc, _job())
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(job: CronJob) -> None:
        calls.append(job.id)
        started.set()
        await release.wait()

    svc.set_on_job(slow)

    async def run():
        first = asyncio.create_task(svc._execute_job(job))
        await started.wait()
        second = await svc._execute_job(job)
        release.set()
        assert await first is True
        return second

    assert asyncio.run(run()) is False
    assert calls == ["job-dedup"]


def test_run_job_refuses_while_running(env):
    svc, calls = env.svc, env.calls
    job = _seed(svc, _job())
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(job: CronJob) -> None:
        calls.append(job.id)
        started.set()
        await release.wait()

    svc.set_on_job(slow)

    async def run():
        task = asyncio.create_task(svc._execute_job(job))
        await started.wait()
        refused = await svc.run_job(job.id, force=True)
        release.set()
        await task
        return refused

    assert asyncio.run(run()) is False
    assert calls == ["job-dedup"]


def test_pre_advance_is_persisted_before_the_callback(env):
    svc = env.svc
    job = _seed(svc, _job())
    seen: list[int | None] = []

    async def check(job: CronJob) -> None:
        data = json.loads(svc.store_path.read_text(encoding="utf-8"))
        seen.append(next(j for j in data["jobs"] if j["id"] == job.id)["state"]["nextRunAtMs"])

    svc.set_on_job(check)

    assert asyncio.run(svc._execute_job(job)) is True
    assert seen == [T0 + EVERY_MS]
    # The post-run path must not advance the same slot again.
    assert job.state.next_run_at_ms == T0 + EVERY_MS


def test_stale_tick_for_a_consumed_slot_is_refused(env):
    svc, calls = env.svc, env.calls
    job = _seed(svc, _job())
    svc._load_store()
    # Simulate another loop having consumed the slot already.
    svc._store.jobs[0].state.next_run_at_ms = T0 + EVERY_MS

    assert asyncio.run(svc._execute_job(job)) is False
    assert calls == []


def test_manual_force_still_consumes_the_pending_slot(env):
    svc, clock = env.svc, env.clock
    job = _seed(svc, _job())
    job.state.next_run_at_ms = T0 + EVERY_MS
    svc._save_store()
    clock.now = T0 + 20_000

    assert asyncio.run(svc.run_job(job.id, force=True)) is True
    reloaded = svc.get_job(job.id)
    assert reloaded is not None
    assert reloaded.state.next_run_at_ms == T0 + 2 * EVERY_MS


def test_crash_after_pre_advance_does_not_refire(env):
    svc = env.svc
    job = _seed(svc, _job())

    async def crash(_job: CronJob) -> None:
        raise RuntimeError("crash after the slot was consumed")

    svc.set_on_job(crash)
    assert asyncio.run(svc._execute_job(job)) is True

    # A fresh service reading the same file sees a future slot: no refire.
    reloaded = cron_base.CronService()
    reloaded.store_path = svc.store_path
    reloaded._load_store()
    reloaded._recompute_next_runs()
    assert reloaded._store.jobs[0].state.next_run_at_ms == T0 + EVERY_MS
