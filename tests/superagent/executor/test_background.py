"""Background job ownership tests."""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

from superagent.executor.background import BackgroundWork, Job


def test_shell_job_holds_no_slot_and_expires_after_30_minutes() -> None:
    work = BackgroundWork()
    job = Job(id="shell-1", ticket_id="t1", kind="shell")
    work.track(job)
    assert work.holds_slot("t1") is False
    assert work.expired_shell_jobs() == []
    future = datetime.now(UTC) + timedelta(minutes=31)
    assert work.expired_shell_jobs(future) == [job]


def test_subagent_job_holds_slot_and_stop_background_clears_all() -> None:
    work = BackgroundWork()
    work.track(Job(id="sub-1", ticket_id="t1", kind="subagent", run_id="run-1"))
    work.track(Job(id="shell-1", ticket_id="t1", kind="shell"))
    assert work.holds_slot("t1") is True
    shells, subagents = work.stop_background("t1")
    assert shells == ["shell-1"] and subagents == ["run-1"]
    assert work.holds_slot("t1") is False
    assert work.running_jobs() == []
