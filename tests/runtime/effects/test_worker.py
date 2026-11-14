"""Worker claim/execute/retry/abandon behaviour, including the generation CAS."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest

from runtime.effects import intent_log, worker as worker_mod
from runtime.effects.worker import EffectResult, EffectWorker

pytestmark = [pytest.mark.unit]


def _status(db_path, intent_id: int) -> tuple[str, int, float]:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT status, attempts, due_at FROM effect_intent WHERE id = ?", (intent_id,)
        ).fetchone()
        assert row is not None
        return str(row[0]), int(row[1]), float(row[2])
    finally:
        conn.close()


def _force_attempts(db_path, intent_id: int, attempts: int) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE effect_intent SET attempts = ? WHERE id = ?", (attempts, intent_id))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_handler_done_marks_the_intent_done(effects_db) -> None:
    intent_id, _ = intent_log.enqueue("kind", "key", {})
    worker = EffectWorker()
    worker.register("kind", lambda payload: _done())

    await worker._tick()

    assert _status(effects_db, intent_id)[0] == "done"


@pytest.mark.asyncio
async def test_intent_log_calls_run_off_the_event_loop(effects_db, monkeypatch) -> None:
    """M9: the blocking sqlite calls are dispatched to worker threads."""
    intent_log.enqueue("kind", "key", {})
    worker = EffectWorker()
    worker.register("kind", lambda payload: _done())
    loop_thread = threading.get_ident()
    seen: list[int] = []

    for name in ("fetch_pending", "mark_in_flight", "mark_done"):
        original = getattr(intent_log, name)

        def _spy(*args, _original=original, **kwargs):
            seen.append(threading.get_ident())
            return _original(*args, **kwargs)

        monkeypatch.setattr(intent_log, name, _spy)

    await worker._tick()

    assert seen
    assert all(thread_id != loop_thread for thread_id in seen)


@pytest.mark.asyncio
async def test_handler_retry_returns_the_intent_to_pending(effects_db) -> None:
    intent_id, _ = intent_log.enqueue("kind", "key", {})
    worker = EffectWorker()
    worker.register("kind", lambda payload: _retry(1))

    await worker._tick()

    status, attempts, due = _status(effects_db, intent_id)
    assert status == "pending"
    assert attempts == 1
    assert due > time.time()


@pytest.mark.asyncio
async def test_handler_abandon_is_terminal(effects_db) -> None:
    intent_id, _ = intent_log.enqueue("kind", "key", {})
    worker = EffectWorker()
    worker.register("kind", lambda payload: _abandon())

    await worker._tick()

    assert _status(effects_db, intent_id)[0] == "abandoned"


@pytest.mark.asyncio
async def test_handler_exception_retries(effects_db) -> None:
    intent_id, _ = intent_log.enqueue("kind", "key", {})
    worker = EffectWorker()

    async def boom(payload):
        raise RuntimeError("handler exploded")

    worker.register("kind", boom)

    await worker._tick()

    status, attempts, _ = _status(effects_db, intent_id)
    assert status == "pending"
    assert attempts == 1


@pytest.mark.asyncio
async def test_a_missing_handler_leaves_the_intent_pending(effects_db) -> None:
    intent_id, _ = intent_log.enqueue("kind", "key", {})

    await EffectWorker()._tick()

    assert _status(effects_db, intent_id)[0] == "pending"


@pytest.mark.asyncio
async def test_max_attempts_abandons_before_running(
    effects_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    intent_id, _ = intent_log.enqueue("kind", "key", {})
    _force_attempts(effects_db, intent_id, 1)
    monkeypatch.setattr(worker_mod, "max_attempts", lambda: 1)
    called = {"n": 0}
    worker = EffectWorker()

    async def handler(payload):
        called["n"] += 1
        return EffectResult(done=True)

    worker.register("kind", handler)

    await worker._tick()

    assert called["n"] == 0
    assert _status(effects_db, intent_id)[0] == "abandoned"


@pytest.mark.asyncio
async def test_two_workers_claim_the_same_intent_once(effects_db) -> None:
    intent_id, _ = intent_log.enqueue("kind", "key", {})
    calls = {"n": 0}

    async def handler(payload):
        calls["n"] += 1
        await asyncio.sleep(0)
        return EffectResult(done=True)

    first = EffectWorker()
    second = EffectWorker()
    first.register("kind", handler)
    second.register("kind", handler)

    await asyncio.gather(first._tick(), second._tick())

    assert calls["n"] == 1
    assert _status(effects_db, intent_id)[0] == "done"


# -- async result helpers ---------------------------------------------------


async def _done() -> EffectResult:
    return EffectResult(done=True)


async def _retry(after: float) -> EffectResult:
    return EffectResult(retry_after_s=after)


async def _abandon() -> EffectResult:
    return EffectResult(abandon=True)
