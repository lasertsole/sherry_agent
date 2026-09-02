"""Task 5 — atomic ``submit_user_input`` entry point (service layer).

Covers the plan spec (input-queueing-reply-binding Task 5):

1. Idle session → ``STARTED`` + exactly ONE ``CLAIMED`` placeholder row
   persisted (the durable "turn in progress" fact, written in the SAME
   critical section as the idle check — kills TOCTOU) + TurnExecutor
   dispatched via ``asyncio.create_task`` AFTER the lock is released.
2. Busy session (ANY busy reason, incl. ``hitl_pending`` /
   ``auto_turn_inflight`` — no special-casing) → ``UserInputQueue.enqueue``
   → ``QUEUED(position)``, executor NOT called.
3. Queue full (both branches) → ``QUEUE_FULL``.
4. Repeated ``client_msg_id`` → ``DEDUPED``, no double-enqueue, no double
   dispatch.
5. Concurrency: two ``asyncio.gather`` submits on an idle session → exactly
   one ``STARTED`` + one ``QUEUED``, exactly one CLAIMED row, executor called
   exactly once (the CLAIMED placeholder makes the second submit see "busy").

Hermetic: fake ``detect_state`` (monkeypatched onto the service module —
submit resolves it as a module global at call time), real ``UserInputQueue``
on a tmp SQLite file, fake TurnExecutors injected through a real
``TurnExecutorRegistry``.
"""

import asyncio
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from agent.tools.subagent.registry.session_state import SessionState
from server.queue.user_input_queue import (
    MAX_ACTIVE_PER_SESSION,
    UserInputQueue,
    UserInputQueueStatus,
)
from server.service import input_queue_service as iqs
from server.service.input_queue_service import (
    SubmitStatus,
    TurnExecutor,
    TurnExecutorRegistry,
    submit_user_input,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeStateDetector:
    """Stand-in for detect_state: returns a fixed busy/reason combination."""

    def __init__(self, busy: bool = False, reason: str = "idle") -> None:
        self.busy = busy
        self.reason = reason
        self.calls: list[str] = []

    def __call__(self, session_key: str) -> SessionState:
        self.calls.append(session_key)
        return SessionState(session_id=session_key, busy=self.busy, reason=self.reason)


class FakeTurnExecutor:
    """Records execute() calls; satisfies the TurnExecutor protocol."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.calls: list[tuple[str, str, str, str | None]] = []

    async def execute(
        self, session_id: str, message: str, source: str, reply_target: str | None
    ) -> None:
        self.calls.append((session_id, message, source, reply_target))


class FakeOutboundRouter:
    """Satisfies the OutboundRouter protocol (contract shape check only)."""

    async def send(self, session_id: str, frame: Any) -> None:  # pragma: no cover
        pass


@pytest.fixture
def detector(monkeypatch) -> FakeStateDetector:
    """Fake detect_state patched onto the service module (idle by default)."""
    det = FakeStateDetector()
    monkeypatch.setattr(iqs, "detect_state", det)
    return det


@pytest.fixture
def store(tmp_path: Path) -> UserInputQueue:
    """Real store on a hermetic tmp SQLite file (same DB layout as production)."""
    return UserInputQueue(db_path=tmp_path / "subagent_registry.db")


@pytest.fixture
def registry() -> TurnExecutorRegistry:
    """Real registry with a ws + a channel fake executor registered."""
    reg = TurnExecutorRegistry()
    reg.register("ws", FakeTurnExecutor("ws-executor"))
    reg.register("channel", FakeTurnExecutor("channel-executor"))
    return reg


@pytest.fixture(autouse=True)
def _clean_session_locks():
    """Keep the module-level per-session lock table small around every test."""
    iqs._SESSION_LOCKS.clear()
    yield
    iqs._SESSION_LOCKS.clear()


async def _settle() -> None:
    """Let fire-and-forget dispatched executor tasks actually run."""
    for _ in range(4):
        await asyncio.sleep(0)


def _ws_executor(registry: TurnExecutorRegistry) -> FakeTurnExecutor:
    return registry.resolve("ws")  # pyright: ignore[reportReturnType]


def _channel_executor(registry: TurnExecutorRegistry) -> FakeTurnExecutor:
    return registry.resolve("channel")  # pyright: ignore[reportReturnType]


# ---------------------------------------------------------------------------
# Idle branch: STARTED + CLAIMED placeholder + executor dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_session_starts_turn_and_persists_claimed_row(
    store: UserInputQueue, registry: TurnExecutorRegistry, detector: FakeStateDetector
):
    result = await submit_user_input(
        "s1", "hello", "user", queue=store, executor_registry=registry
    )

    assert result.status is SubmitStatus.STARTED
    rows = await store.list_active("s1")
    assert len(rows) == 1, "exactly one placeholder row must be persisted"
    assert rows[0].status is UserInputQueueStatus.CLAIMED
    assert json.loads(rows[0].payload)["text"] == "hello"
    assert rows[0].source == "user"
    assert rows[0].reply_target is None

    await _settle()
    ws = _ws_executor(registry)
    assert ws.calls == [("s1", "hello", "user", None)], "executor dispatched once"
    assert _channel_executor(registry).calls == []


@pytest.mark.asyncio
async def test_cron_source_passes_through_to_queue_and_executor(
    store: UserInputQueue, registry: TurnExecutorRegistry, detector: FakeStateDetector
):
    """source=cron is fixed at the call site and passes through unchanged (G2)."""
    result = await submit_user_input(
        "s1", "proactive nudge", "cron", queue=store, executor_registry=registry
    )

    assert result.status is SubmitStatus.STARTED
    rows = await store.list_active("s1")
    assert rows[0].source == "cron"
    await _settle()
    assert _ws_executor(registry).calls == [("s1", "proactive nudge", "cron", None)]


@pytest.mark.asyncio
async def test_channel_reply_target_resolves_channel_executor(
    store: UserInputQueue, registry: TurnExecutorRegistry, detector: FakeStateDetector
):
    reply_target = '{"channel": "qq", "chat_id": "42"}'
    result = await submit_user_input(
        "s1",
        "hi",
        "user",
        reply_target=reply_target,
        queue=store,
        executor_registry=registry,
    )

    assert result.status is SubmitStatus.STARTED
    rows = await store.list_active("s1")
    assert rows[0].reply_target == reply_target
    await _settle()
    assert _channel_executor(registry).calls == [("s1", "hi", "user", reply_target)]
    assert _ws_executor(registry).calls == [], "ws route must not be used"


@pytest.mark.asyncio
async def test_missing_executor_registration_raises_without_mutating_queue(
    store: UserInputQueue, registry: TurnExecutorRegistry, detector: FakeStateDetector
):
    """Idle dispatch with no executor for the route fails fast BEFORE any state write."""
    bare_registry = TurnExecutorRegistry()  # nothing registered
    with pytest.raises(RuntimeError, match="ws"):
        await submit_user_input(
            "s1", "hello", "user", queue=store, executor_registry=bare_registry
        )

    assert await store.count_active("s1") == 0, "no placeholder row may be orphaned"
    assert detector.calls, "state was detected before failing"


# ---------------------------------------------------------------------------
# Busy branch: enqueue → QUEUED(position), executor skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_busy_session_enqueues_with_position_and_skips_executor(
    store: UserInputQueue, registry: TurnExecutorRegistry, monkeypatch
):
    detector = FakeStateDetector(busy=True, reason="answering")
    monkeypatch.setattr(iqs, "detect_state", detector)

    await store.enqueue("s1", '{"text": "earlier", "image_base64_list": []}', "user")
    result = await submit_user_input(
        "s1", "hello", "user", queue=store, executor_registry=registry
    )

    assert result.status is SubmitStatus.QUEUED
    assert result.position == 2, "1-based FIFO position among QUEUED rows"
    rows = await store.list_active("s1")
    assert [r.status for r in rows] == [
        UserInputQueueStatus.QUEUED,
        UserInputQueueStatus.QUEUED,
    ]
    assert json.loads(rows[-1].payload)["text"] == "hello"

    await _settle()
    assert _ws_executor(registry).calls == [], "busy session must NOT start a turn"


@pytest.mark.asyncio
async def test_hitl_pending_session_enqueues_without_competing_turn(
    store: UserInputQueue, registry: TurnExecutorRegistry, monkeypatch
):
    """HITL wait is busy (Task 2 signal): the submit queues instead of racing."""
    detector = FakeStateDetector(busy=True, reason="hitl_pending")
    monkeypatch.setattr(iqs, "detect_state", detector)

    result = await submit_user_input(
        "s1", "answer pending", "user", queue=store, executor_registry=registry
    )

    assert result.status is SubmitStatus.QUEUED
    assert result.position == 1
    rows = await store.list_active("s1")
    assert len(rows) == 1
    assert rows[0].status is UserInputQueueStatus.QUEUED, "no competing CLAIMED row"
    assert rows[0].source == "user"

    await _settle()
    assert _ws_executor(registry).calls == []


@pytest.mark.asyncio
async def test_auto_turn_inflight_session_enqueues_like_any_busy_reason(
    store: UserInputQueue, registry: TurnExecutorRegistry, monkeypatch
):
    """ANY busy=True reason queues — submit never special-cases reasons (G2)."""
    detector = FakeStateDetector(busy=True, reason="auto_turn_inflight")
    monkeypatch.setattr(iqs, "detect_state", detector)

    result = await submit_user_input(
        "s1", "hello", "user", queue=store, executor_registry=registry
    )

    assert result.status is SubmitStatus.QUEUED
    await _settle()
    assert _ws_executor(registry).calls == []


@pytest.mark.asyncio
async def test_busy_session_queue_full_returns_queue_full(
    store: UserInputQueue, registry: TurnExecutorRegistry, monkeypatch
):
    detector = FakeStateDetector(busy=True, reason="answering")
    monkeypatch.setattr(iqs, "detect_state", detector)

    for i in range(MAX_ACTIVE_PER_SESSION):
        await store.enqueue("s1", f'{{"text": "m{i}", "image_base64_list": []}}', "user")

    result = await submit_user_input(
        "s1", "overflow", "user", queue=store, executor_registry=registry
    )

    assert result.status is SubmitStatus.QUEUE_FULL
    assert await store.count_active("s1") == MAX_ACTIVE_PER_SESSION, "cap holds"
    await _settle()
    assert _ws_executor(registry).calls == []


# ---------------------------------------------------------------------------
# Dedup: repeated client_msg_id → DEDUPED, no double-enqueue / double dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_client_msg_id_busy_returns_deduped(
    store: UserInputQueue, registry: TurnExecutorRegistry, monkeypatch
):
    detector = FakeStateDetector(busy=True, reason="answering")
    monkeypatch.setattr(iqs, "detect_state", detector)

    first = await submit_user_input(
        "s1", "hello", "user", client_msg_id="m-1", queue=store, executor_registry=registry
    )
    assert first.status is SubmitStatus.QUEUED

    retry = await submit_user_input(
        "s1", "hello", "user", client_msg_id="m-1", queue=store, executor_registry=registry
    )
    assert retry.status is SubmitStatus.DEDUPED
    assert await store.count_active("s1") == 1, "no double-enqueue"
    await _settle()
    assert _ws_executor(registry).calls == []


@pytest.mark.asyncio
async def test_duplicate_client_msg_id_inflight_claimed_returns_deduped_no_double_executor(
    store: UserInputQueue, registry: TurnExecutorRegistry, detector: FakeStateDetector
):
    """Retrying the SAME msg while the CLAIMED placeholder is in flight dedups."""
    first = await submit_user_input(
        "s1", "hello", "user", client_msg_id="m-1", queue=store, executor_registry=registry
    )
    assert first.status is SubmitStatus.STARTED

    retry = await submit_user_input(
        "s1", "hello", "user", client_msg_id="m-1", queue=store, executor_registry=registry
    )
    assert retry.status is SubmitStatus.DEDUPED
    assert await store.count_active("s1") == 1
    await _settle()
    assert len(_ws_executor(registry).calls) == 1, "no double dispatch"


# ---------------------------------------------------------------------------
# Concurrency: atomic window (lock + CLAIMED placeholder in same critical section)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_submits_on_idle_session_yield_one_started_one_queued(
    store: UserInputQueue, registry: TurnExecutorRegistry, detector: FakeStateDetector
):
    """AC-3: two gathers on an idle session → {STARTED, QUEUED}, 1 CLAIMED row."""
    results = await asyncio.gather(
        submit_user_input("s1", "hello", "user", queue=store, executor_registry=registry),
        submit_user_input("s1", "world", "user", queue=store, executor_registry=registry),
    )

    statuses = sorted(r.status.value for r in results)
    assert statuses == ["QUEUED", "STARTED"], f"got {statuses}"
    queued = [r for r in results if r.status is SubmitStatus.QUEUED][0]
    assert queued.position == 1

    rows = await store.list_active("s1")
    assert len(rows) == 2
    claimed = [r for r in rows if r.status is UserInputQueueStatus.CLAIMED]
    assert len(claimed) == 1, "exactly one CLAIMED placeholder"
    assert json.loads(claimed[0].payload)["text"] in ("hello", "world")

    await _settle()
    ws = _ws_executor(registry)
    assert len(ws.calls) == 1, "executor called exactly once"
    assert ws.calls[0][1] == json.loads(claimed[0].payload)["text"]


@pytest.mark.asyncio
async def test_concurrent_duplicate_submits_yield_single_dispatch(
    store: UserInputQueue, registry: TurnExecutorRegistry, detector: FakeStateDetector
):
    """Same client_msg_id gathered twice → {STARTED, DEDUPED}, one row, one call."""
    results = await asyncio.gather(
        submit_user_input(
            "s1", "hello", "user", client_msg_id="m-1", queue=store, executor_registry=registry
        ),
        submit_user_input(
            "s1", "hello", "user", client_msg_id="m-1", queue=store, executor_registry=registry
        ),
    )

    statuses = sorted(r.status.value for r in results)
    assert statuses == ["DEDUPED", "STARTED"], f"got {statuses}"
    assert await store.count_active("s1") == 1
    rows = await store.list_active("s1")
    assert rows[0].status is UserInputQueueStatus.CLAIMED
    await _settle()
    assert len(_ws_executor(registry).calls) == 1


# ---------------------------------------------------------------------------
# Queue-full on the idle branch (CLAIMED placeholder respects the cap)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_claimed_placeholder_respects_queue_cap(
    store: UserInputQueue, registry: TurnExecutorRegistry, detector: FakeStateDetector
):
    for i in range(MAX_ACTIVE_PER_SESSION):
        await store.enqueue("s1", f'{{"text": "m{i}", "image_base64_list": []}}', "user")

    result = await submit_user_input(
        "s1", "overflow", "user", queue=store, executor_registry=registry
    )

    assert result.status is SubmitStatus.QUEUE_FULL
    assert await store.count_active("s1") == MAX_ACTIVE_PER_SESSION
    await _settle()
    assert _ws_executor(registry).calls == [], "no turn started when full"


# ---------------------------------------------------------------------------
# Contracts for Tasks 7/9: registry + protocols
# ---------------------------------------------------------------------------


def test_turn_executor_registry_register_and_resolve():
    registry = TurnExecutorRegistry()
    executor = FakeTurnExecutor()
    registry.register("ws", executor)

    assert registry.resolve("ws") is executor
    assert registry.resolve("channel") is None, "unregistered route resolves to None"

    # protocol shapes (runtime-checkable): fakes satisfy both contracts
    assert isinstance(executor, TurnExecutor)
    assert isinstance(FakeOutboundRouter(), iqs.OutboundRouter)


# ---------------------------------------------------------------------------
# Caller-bug guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_session_id_raises_value_error(
    store: UserInputQueue, registry: TurnExecutorRegistry, detector: FakeStateDetector
):
    with pytest.raises(ValueError, match="session_id"):
        await submit_user_input(
            "", "hello", "user", queue=store, executor_registry=registry
        )
    assert await store.count_active("") == 0
    await _settle()
    assert _ws_executor(registry).calls == []


@pytest.mark.asyncio
async def test_invalid_source_raises_value_error(
    store: UserInputQueue, registry: TurnExecutorRegistry, detector: FakeStateDetector
):
    with pytest.raises(ValueError, match="source"):
        await submit_user_input(
            "s1", "hello", "system", queue=store, executor_registry=registry  # pyright: ignore[reportArgumentType]
        )
    assert await store.count_active("s1") == 0
