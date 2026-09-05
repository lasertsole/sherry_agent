"""Unit tests for the cron failure-tracking breaker (plan Task 4, design §5.3).

Covers: consecutive-failure counting, exponential backoff degradation at
``DEGRADED_THRESHOLD``, auto-disable + persistence at ``DISABLED_THRESHOLD``,
record-then-re-raise semantics, all three reset paths (success / ``enable_job``
/ ``reset_failures``), the public breaker API and the ``channel=None``
notification degradation.

Isolation iron rules:
- fresh ``CronService()`` per test; the store file lives under ``tmp_path``
  (the real ``cron_jobs.json`` is NEVER touched);
- the module-level ``cron_service`` singleton is never used and ``start()``
  is never called (no threads, no event loops beyond ``asyncio.run``);
- every patch goes through ``monkeypatch`` (self-restoring; ``tests/unit``
  runs in a single pytest process).
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from skills.builtin.core.cron.scripts import base as cron_base
from skills.builtin.core.cron.scripts.types import CronJob, CronPayload, CronSchedule


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, content: str = "cron-ok") -> None:
        self.content = content


class _FakeAgent:
    """Fake compiled agent: ``ainvoke`` raises the configured exception or succeeds."""

    def __init__(self) -> None:
        self.behavior: Exception | None = None
        self.calls = 0

    async def ainvoke(self, input):  # noqa: ANN001 - signature mirrors langchain
        self.calls += 1
        if self.behavior is not None:
            raise self.behavior
        return {"messages": [_FakeMessage()]}


class _FakeBus:
    def __init__(self) -> None:
        self.inbound: list[object] = []
        self.published: list[object] = []

    async def publish_inbound(self, msg) -> None:  # noqa: ANN001
        self.inbound.append(msg)

    async def publish_outbound(self, msg) -> None:  # noqa: ANN001
        self.published.append(msg)


class _FakeChannelManager:
    def __init__(self, bus: _FakeBus) -> None:
        self._bus = bus

    def get_bus(self) -> _FakeBus:
        return self._bus


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Fresh CronService with a tmp store and a fully faked agent call path."""
    bus = _FakeBus()
    fake_agent = _FakeAgent()
    fake_agent.behavior = RuntimeError("boom-fake")  # default: every run fails

    # Redirect every on-disk side effect (store + execution logs) into tmp_path.
    monkeypatch.setattr(cron_base, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(cron_base, "channel_manager", _FakeChannelManager(bus))
    monkeypatch.setattr(cron_base, "build_main_llm", lambda: None)
    monkeypatch.setattr(cron_base, "build_system_prompt", lambda: "sp")
    monkeypatch.setattr(cron_base, "create_agent", lambda **kwargs: fake_agent)

    # _on_cron_job imports the tool builders lazily from agent.tools at call time.
    import agent.tools as agent_tools

    for name in ("build_python_repl_tool", "build_read_file_tool", "build_write_file_tool"):
        monkeypatch.setattr(agent_tools, name, lambda: SimpleNamespace(metadata={}))

    svc = cron_base.CronService()
    svc.store_path = tmp_path / "cron_jobs.json"
    svc.set_on_job(svc._on_cron_job)  # mirror init(): _execute_job -> _on_cron_job

    return SimpleNamespace(svc=svc, bus=bus, agent=fake_agent, tmp_path=tmp_path)


def _add_job(env, *, channel: str | None = "qq", to: str | None = "u1", kind: str = "every", enabled: bool = True) -> CronJob:
    """Insert one recurring job into the tmp store and persist it."""
    if kind == "at":
        schedule = CronSchedule(kind="at", at_ms=4102444800000)  # far future
    else:
        schedule = CronSchedule(kind="every", every_ms=60_000)
    job = CronJob(
        id="job00001",
        name="flaky",
        enabled=enabled,
        schedule=schedule,
        payload=CronPayload(deliver=True, message="do things", channel=channel, to=to),
    )
    env.svc._load_store()
    env.svc._store.jobs.append(job)
    env.svc._save_store()
    return job


def _fail(env, job, times: int = 1) -> None:
    """Drive ``times`` failures through _on_cron_job; each must re-raise.

    If the job is degraded, the backoff window is treated as fully elapsed
    before each attempt (the real scheduler only refires after the window),
    so consecutive calls actually reach the agent.
    """
    for _ in range(times):
        state = env.svc._failure_states.get(job.id)
        if state is not None and state.degraded_since is not None:
            state.degraded_since = time.monotonic() - 10_000  # window long past
        with pytest.raises(RuntimeError):
            asyncio.run(env.svc._on_cron_job(job))


def _store_enabled(env) -> bool | None:
    data = json.loads(env.svc.store_path.read_text(encoding="utf-8"))
    return next(j["enabled"] for j in data["jobs"] if j["id"] == "job00001")


# ---------------------------------------------------------------------------
# Degrade / disable (happy path)
# ---------------------------------------------------------------------------


def test_degrade_at_5_with_base_backoff(env):
    job = _add_job(env)
    _fail(env, job, times=5)
    state = env.svc.get_failure_state(job.id)
    assert state is not None
    assert state["consecutive_failures"] == 5
    assert state["backoff_ms"] == env.svc.DEGRADE_BACKOFF_BASE_MS == 5000
    assert state["degraded_since"] is not None
    assert state["last_error"] == "boom-fake"
    assert job.enabled is True


def test_backoff_grows_exponentially_and_caps(env):
    """Backoff doubles per failure past the threshold, capped at the max.

    Drives ``_record_failure`` directly: once degraded, the entry gate of
    ``_on_cron_job`` would (by design) skip further immediate runs, so the
    pure transition logic is exercised here instead.
    """
    job = _add_job(env)
    expected = [5000, 10000, 20000, 40000, 80000, 160000]
    for i in range(1, 7):
        asyncio.run(env.svc._record_failure(job, "boom-fake"))
        if i < 5:
            assert env.svc.get_failure_state(job.id)["backoff_ms"] == 0, f"failure #{i}"
        else:
            want = expected[i - 5]
            assert env.svc.get_failure_state(job.id)["backoff_ms"] == want, f"failure #{i}"
    # Failures 7-10 keep doubling (pre-cap): 20000, 40000, 80000, 160000.
    for want in (20000, 40000, 80000, 160000):
        asyncio.run(env.svc._record_failure(job, "boom-fake"))
        assert env.svc.get_failure_state(job.id)["backoff_ms"] == want
    # The cap kicks in (5000 * 2**6 = 320000 > 300000) and never grows further.
    for _ in range(4):
        asyncio.run(env.svc._record_failure(job, "boom-fake"))
    assert env.svc.get_failure_state(job.id)["backoff_ms"] == env.svc.DEGRADE_BACKOFF_MAX_MS
    assert env.svc.get_failure_state(job.id)["consecutive_failures"] == 14


def test_disable_at_10_flips_enabled_and_persists(env):
    job = _add_job(env)
    _fail(env, job, times=10)
    assert job.enabled is False
    assert _store_enabled(env) is False  # persisted to the tmp store JSON
    state = env.svc.get_failure_state(job.id)
    assert state["consecutive_failures"] == 10


def test_exception_is_reraised_record_then_raise(env):
    job = _add_job(env)
    env.agent.behavior = RuntimeError("boom-fake")
    with pytest.raises(RuntimeError, match="boom-fake"):
        asyncio.run(env.svc._on_cron_job(job))
    # The failure was recorded *before* the re-raise.
    assert env.svc.get_failure_state(job.id)["consecutive_failures"] == 1


def test_execute_job_path_records_last_status_error(env):
    """Re-raise must keep _execute_job's last_status="error" contract intact."""
    job = _add_job(env)
    env.agent.behavior = RuntimeError("boom-fake")
    asyncio.run(env.svc._execute_job(job))
    assert job.state.last_status == "error"
    assert job.state.last_error == "boom-fake"
    assert env.svc.get_failure_state(job.id)["consecutive_failures"] == 1


def test_degraded_backoff_gate_skips_execution(env):
    """Inside the backoff window the agent call is skipped (no raise, no count)."""
    job = _add_job(env)
    _fail(env, job, times=5)
    env.agent.behavior = None  # would succeed if executed
    result = asyncio.run(env.svc._on_cron_job(job))
    assert result is None
    assert env.agent.calls == 5  # the 6th run never reached the agent
    assert env.svc.get_failure_state(job.id)["consecutive_failures"] == 5


def test_disabled_job_short_circuits(env):
    job = _add_job(env, enabled=False)
    env.agent.behavior = RuntimeError("boom-fake")
    result = asyncio.run(env.svc._on_cron_job(job))
    assert result is None
    assert env.agent.calls == 0
    assert env.svc.get_failure_state(job.id) is None


def test_at_job_is_not_counted_by_breaker(env):
    """One-shot `at` jobs keep their _execute_job handling; no breaker counting."""
    job = _add_job(env, kind="at")
    env.agent.behavior = RuntimeError("boom-fake")
    asyncio.run(env.svc._execute_job(job))
    assert job.state.last_status == "error"
    assert env.svc.get_failure_state(job.id) is None


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------


def test_disable_notification_published_to_payload_channel(env):
    job = _add_job(env, channel="qq", to="u1")
    _fail(env, job, times=10)
    assert len(env.bus.published) == 1
    msg = env.bus.published[0]
    assert msg.channel == "qq"
    assert msg.chat_id == "u1"
    assert msg.metadata == {"job_id": job.id}
    assert "auto-disabled" in msg.content
    assert str(10) in msg.content


def test_channel_none_degrades_to_log_only(env):
    job = _add_job(env, channel=None, to=None)
    _fail(env, job, times=10)  # must not raise anything else / crash
    assert job.enabled is False
    assert env.bus.published == []
    assert _store_enabled(env) is False


# ---------------------------------------------------------------------------
# Reset paths + public API
# ---------------------------------------------------------------------------


def test_success_resets_failure_state(env):
    job = _add_job(env)
    _fail(env, job, times=3)
    env.agent.behavior = None
    asyncio.run(env.svc._on_cron_job(job))
    state = env.svc.get_failure_state(job.id)
    assert state == {
        "consecutive_failures": 0,
        "last_error": "",
        "degraded_since": None,
        "backoff_ms": 0,
    }


def test_enable_job_resets_failure_state(env):
    job = _add_job(env)
    _fail(env, job, times=10)
    assert job.enabled is False
    restored = env.svc.enable_job(job.id)
    assert restored is not None and restored.enabled is True
    state = env.svc.get_failure_state(job.id)
    assert state["consecutive_failures"] == 0
    assert state["backoff_ms"] == 0
    assert state["degraded_since"] is None


def test_reset_failures_restores_breaker_disabled_job(env):
    job = _add_job(env)
    _fail(env, job, times=10)
    assert job.enabled is False

    assert env.svc.reset_failures(job.id) is True
    restored = env.svc.get_job(job.id)  # re-fetch: _load_store may reload from disk
    assert restored.enabled is True
    assert _store_enabled(env) is True
    state = env.svc.get_failure_state(job.id)
    assert state["consecutive_failures"] == 0
    assert state["backoff_ms"] == 0
    # Re-enabling mirrors enable_job: next run is recomputed.
    assert restored.state.next_run_at_ms is not None


def test_reset_failures_keeps_manual_disable(env):
    """A manually disabled job with few failures is NOT re-enabled by the reset."""
    job = _add_job(env)
    _fail(env, job, times=2)
    env.svc.enable_job(job.id, enabled=False)
    assert env.svc.reset_failures(job.id) is True
    assert env.svc.get_job(job.id).enabled is False  # manual disable preserved
    assert env.svc.get_failure_state(job.id)["consecutive_failures"] == 0


def test_reset_failures_unknown_job_returns_false(env):
    assert env.svc.reset_failures("no-such-id") is False


def test_get_failure_state_unknown_job_returns_none(env):
    assert env.svc.get_failure_state("no-such-id") is None


def test_failure_state_never_persisted_to_store(env):
    """Breaker state is memory-only: the store schema stays untouched."""
    job = _add_job(env)
    _fail(env, job, times=6)
    data = json.loads(env.svc.store_path.read_text(encoding="utf-8"))
    assert set(data["jobs"][0].keys()) == {
        "id", "name", "enabled", "schedule", "payload", "state",
        "createdAtMs", "updatedAtMs", "deleteAfterRun",
    }
