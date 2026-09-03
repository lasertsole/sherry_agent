"""Task 9 — channel/QQ consumer integration with submit_user_input (TDD).

Tests load ``server/trigger/channels/core.py`` in isolation:

* ``channels`` is replaced by a fake package (``BaseChannel`` + a fake
  ``channel_manager``) so no real IO thread/loop is ever created.
* ``skills.builtin.core.heartbeat`` is replaced by a fake heartbeat service
  whose ``start()`` coroutine parks forever.
* The module is executed under the synthetic name
  ``_channel_core_under_test`` via ``spec_from_file_location``.

Because ``spec_from_file_location`` executes the real module body, these
tests also verify the module-level wiring:

* ``register_channel_turn_infra()`` binds the channel ``TurnExecutor``
  (route ``"channel"``) into the Task 5 registry and registers the channel
  ``OutboundRouter`` with the Task 7 turn runner (lazy seam).
* ``channel_manager.set_inbound_consumer(core._process_inbound)`` is set.
* The queueing contract: ``_process_inbound`` calls
  ``iqs.submit_user_input`` with source marking (G2: ``"cron"`` iff
  ``sender_id == "cron tool"``, never inferred from metadata) and a
  ``reply_target`` JSON blob captured at enqueue time.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pub_func import string_to_unique_int
from runtime.relation_register import relation_register
from server.queue.user_input_queue import UserInputQueue, UserInputQueueStatus
from server.service import input_queue_service as iqs

CORE_PATH = Path(__file__).resolve().parents[3] / "server" / "trigger" / "channels" / "core.py"

pytestmark = pytest.mark.unit


def _payload(text: str) -> str:
    """Task 5 payload convention: text-only JSON blob."""
    return json.dumps({"text": text, "image_base64_list": []}, ensure_ascii=False)


def _reply_target(chat_id: str, message_id: str | None, channel: str = "qq") -> str:
    return json.dumps(
        {"channel": channel, "chat_id": chat_id, "message_id": message_id},
        ensure_ascii=False,
    )


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    """Poll until predicate() is truthy or timeout expires (T7 pattern)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("condition not met in time")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Session-scoped loader: real core.py, fake `channels` + fake heartbeat.
# ---------------------------------------------------------------------------


class _FakeBaseChannel:
    name: str = "fake"


class _FakeChannelManager:
    """Duck-typed stand-in for channels.channel_manager (no IO at all)."""

    def __init__(self) -> None:
        self.channels: dict[str, object] = {}
        self.inbound_consumer = None
        self.outbound_consumer = None
        self.started = False

    def set_inbound_consumer(self, consumer) -> None:
        self.inbound_consumer = consumer

    def set_outbound_consumer(self, consumer) -> None:
        self.outbound_consumer = consumer

    def get_channel(self, name: str):
        return self.channels.get(name)

    def start_service(self) -> None:
        self.started = True

    def get_event_loop(self) -> asyncio.AbstractEventLoop:
        # Never driven by run_forever() unless core starts its thread; the
        # loop exists only so run_coroutine_threadsafe() has a target.
        return asyncio.new_event_loop()


@pytest.fixture(scope="session")
def core():
    """Load the real core.py under a synthetic name with stubbed deps."""
    saved: dict[str, types.ModuleType | None] = {}

    fake_channels: Any = types.ModuleType("channels")
    fake_channels.BaseChannel = _FakeBaseChannel
    fake_channels.channel_manager = _FakeChannelManager()

    fake_heartbeat: Any = types.ModuleType("skills.builtin.core.heartbeat")
    fake_service = types.SimpleNamespace()

    async def _park_forever() -> None:  # pragma: no cover - parked daemon
        await asyncio.Event().wait()

    fake_service.start = _park_forever
    fake_heartbeat.heartbeat_service = fake_service

    saved["channels"] = sys.modules.get("channels")
    saved["skills.builtin.core.heartbeat"] = sys.modules.get("skills.builtin.core.heartbeat")
    sys.modules["channels"] = fake_channels
    sys.modules["skills.builtin.core.heartbeat"] = fake_heartbeat
    try:
        spec = importlib.util.spec_from_file_location("_channel_core_under_test", CORE_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["_channel_core_under_test"] = module
        spec.loader.exec_module(module)
        try:
            yield module
        finally:
            sys.modules.pop("_channel_core_under_test", None)
    finally:
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


# ---------------------------------------------------------------------------
# Per-test environment: hermetic queue, fake AI, stub turn runner.
# ---------------------------------------------------------------------------


class _FakeChannel:
    name = "qq"

    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, message) -> None:
        self.sent.append(message)


def _make_stub_turn_runner():
    stub: Any = types.ModuleType("server.service.turn_runner")
    stub.registered_routers = []
    stub.finished = []
    stub.finished_event = asyncio.Event()

    async def on_turn_finished(session_id, claim_row_id=None):  # noqa: ANN001
        stub.finished.append((session_id, claim_row_id))
        stub.finished_event.set()

    def register_outbound_router(route, router):  # noqa: ANN001
        stub.registered_routers.append((route, router))

    stub.on_turn_finished = on_turn_finished
    stub.register_outbound_router = register_outbound_router
    return stub


@pytest.fixture()
def channel_env(core, tmp_path, monkeypatch):  # noqa: ARG001 - core pins module scope
    store = UserInputQueue(db_path=tmp_path / "channel_queue.db")
    iqs._SESSION_LOCKS.clear()
    monkeypatch.setattr(iqs, "get_default_queue", lambda: store)

    detector = {"busy": False}

    def fake_detect_state(session_key: str):
        return SimpleNamespace(
            session_id=session_key,
            busy=detector["busy"],
            reason="answering" if detector["busy"] else "idle",
        )

    monkeypatch.setattr(iqs, "detect_state", fake_detect_state)

    generate_calls: list[tuple[str, str]] = []

    async def fake_generate(session_id, multi_modal_message, is_stream=True, origin=None):  # noqa: ANN001
        generate_calls.append((session_id, multi_modal_message.text))
        yield {"type": "text", "content": f"echo:{multi_modal_message.text}"}

    monkeypatch.setattr(core, "async_generate", fake_generate)

    stub_runner = _make_stub_turn_runner()
    monkeypatch.setattr(core, "_get_turn_runner", lambda: stub_runner)

    fake_channel = _FakeChannel()
    core.channel_manager.channels.clear()
    core.channel_manager.channels["qq"] = fake_channel

    sid = str(string_to_unique_int("qq"))

    def _cleanup() -> None:
        relation_register.unregister_channel_chat_by_session_id(sid)
        core.channel_manager.channels.clear()
        iqs._SESSION_LOCKS.clear()

    return SimpleNamespace(
        store=store,
        detector=detector,
        generate_calls=generate_calls,
        stub_runner=stub_runner,
        fake_channel=fake_channel,
        sid=sid,
        cleanup=_cleanup,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_message_starts_turn_and_reply_goes_to_captured_reply_target(core, channel_env):
    """Idle user message -> STARTED -> executor drives turn -> reply routed to chat-A."""
    env = channel_env
    message = SimpleNamespace(
        channel="qq",
        sender_id="user-1",
        chat_id="chat-A",
        content="hello",
        timestamp=0,
        media=None,
        metadata={"message_id": "m1"},
        session_id=None,
    )

    await core._process_inbound(message, env.fake_channel)
    await _wait_until(lambda: env.stub_runner.finished)

    # Turn driven through the AI service with the queued text.
    assert env.generate_calls == [(env.sid, "hello")]

    # Reply captured at enqueue time (chat-A), passive msg_id preserved.
    assert len(env.fake_channel.sent) == 1
    out = env.fake_channel.sent[0]
    assert out.channel == "qq"
    assert out.chat_id == "chat-A"
    assert out.content == "echo:hello"
    assert out.metadata == {"message_id": "m1"}

    # Row went through the store: CLAIMED placeholder bound to the turn.
    rows = await env.store.list_active(env.sid)
    assert len(rows) == 1
    assert rows[0].source == "user"
    assert rows[0].reply_target == _reply_target("chat-A", "m1")

    # Drain seam notified with the CLAIMED row id.
    assert env.stub_runner.finished == [(env.sid, rows[0].id)]


@pytest.mark.asyncio
async def test_cron_sender_id_maps_to_source_cron_row(core, channel_env):
    """Cron tool messages queue with source='cron' and no client_msg_id (busy -> QUEUED)."""
    env = channel_env
    env.detector["busy"] = True
    message = SimpleNamespace(
        channel="qq",
        sender_id="cron tool",
        chat_id="chat-cron",
        content="cron result",
        timestamp=0,
        media=None,
        metadata={},
        session_id=None,
    )

    await core._process_inbound(message, env.fake_channel)

    rows = await env.store.list_active(env.sid)
    assert len(rows) == 1
    assert rows[0].source == "cron"
    assert rows[0].client_msg_id is None
    assert rows[0].reply_target == _reply_target("chat-cron", None)
    assert rows[0].status == UserInputQueueStatus.QUEUED
    # No turn dispatched for a queued-only message.
    assert env.generate_calls == []
    assert env.fake_channel.sent == []
    assert env.stub_runner.finished == []


@pytest.mark.asyncio
async def test_user_source_is_never_inferred_from_metadata(core, channel_env):
    """G2: source must come from sender_id only — metadata['source'] is ignored."""
    env = channel_env
    env.detector["busy"] = True
    message = SimpleNamespace(
        channel="qq",
        sender_id="user-2",
        chat_id="chat-A",
        content="hi",
        timestamp=0,
        media=None,
        metadata={"source": "cron", "message_id": "m2"},
        session_id=None,
    )

    await core._process_inbound(message, env.fake_channel)

    rows = await env.store.list_active(env.sid)
    assert len(rows) == 1
    assert rows[0].source == "user"
    assert rows[0].client_msg_id == "m2"


@pytest.mark.asyncio
async def test_busy_session_queues_row_with_source_and_reply_target_persisted(core, channel_env):
    """Busy -> QUEUED row keeps source + reply_target JSON; turn NOT dispatched."""
    env = channel_env
    env.detector["busy"] = True
    message = SimpleNamespace(
        channel="qq",
        sender_id="user-3",
        chat_id="chat-A",
        content="queued text",
        timestamp=0,
        media=None,
        metadata={"message_id": "m3"},
        session_id=None,
    )

    await core._process_inbound(message, env.fake_channel)

    rows = await env.store.list_active(env.sid)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == UserInputQueueStatus.QUEUED
    assert row.source == "user"
    assert row.reply_target == _reply_target("chat-A", "m3")
    assert row.payload == _payload("queued text")
    assert row.client_msg_id == "m3"
    # Nothing dispatched or sent for a queued message.
    assert env.generate_calls == []
    assert env.fake_channel.sent == []
    assert env.stub_runner.finished == []


@pytest.mark.asyncio
async def test_drain_turn_outbound_targets_enqueue_time_reply_target(channel_env):
    """Executor rebuilds the target from reply_target JSON, NOT the live relation map."""
    env = channel_env
    # Live relation map points at chat-A; enqueue-time target is chat-B.
    relation_register.register_channel_chat(env.sid, "qq", "chat-A")

    executor = iqs.get_default_registry().resolve("channel")
    assert executor is not None

    await executor.execute(
        env.sid,
        "queued text",
        "user",
        _reply_target("chat-B", None),
    )
    await _wait_until(lambda: env.stub_runner.finished)

    assert len(env.fake_channel.sent) == 1
    out = env.fake_channel.sent[0]
    assert out.channel == "qq"
    assert out.chat_id == "chat-B"
    assert out.content == "echo:queued text"
    assert out.metadata == {}

    # No placeholder row existed -> claim_row_id None on the drain seam.
    assert env.stub_runner.finished == [(env.sid, None)]


@pytest.mark.asyncio
async def test_queue_full_cron_drop_does_not_crash_consumer(core, channel_env):
    """QUEUE_FULL on a cron message logs + drops; consumer never raises."""
    env = channel_env
    from server.queue.user_input_queue import MAX_ACTIVE_PER_SESSION

    for i in range(MAX_ACTIVE_PER_SESSION):
        await env.store.enqueue(env.sid, _payload(f"pre-{i}"), "user")
    assert await env.store.count_active(env.sid) == MAX_ACTIVE_PER_SESSION

    env.detector["busy"] = True
    message = SimpleNamespace(
        channel="qq",
        sender_id="cron tool",
        chat_id="chat-cron",
        content="cron result",
        timestamp=0,
        media=None,
        metadata={},
        session_id=None,
    )

    # Must not raise even though the store is at capacity.
    await core._process_inbound(message, env.fake_channel)

    assert await env.store.count_active(env.sid) == MAX_ACTIVE_PER_SESSION
    assert env.fake_channel.sent == []
    assert env.generate_calls == []
    assert env.stub_runner.finished == []


@pytest.mark.asyncio
async def test_module_registration_binds_channel_executor_and_outbound_router(core):
    """register_channel_turn_infra() binds executor into registry + router into turn runner."""
    stub_runner = _make_stub_turn_runner()
    original_get_turn_runner = core._get_turn_runner
    core._get_turn_runner = lambda: stub_runner
    try:
        core.register_channel_turn_infra()
    finally:
        core._get_turn_runner = original_get_turn_runner  # restore real lazy seam

    # Executor bound under the channel route.
    executor = iqs.get_default_registry().resolve("channel")
    assert isinstance(executor, core._ChannelTurnExecutor)

    # Outbound router registered on the (stub) turn runner exactly once per call.
    assert len(stub_runner.registered_routers) == 1
    route, router = stub_runner.registered_routers[0]
    assert route == "channel"
    assert isinstance(router, core._ChannelOutboundRouter)

    # Inbound consumer still wired at module level.
    assert core.channel_manager.inbound_consumer is core._process_inbound


@pytest.mark.asyncio
async def test_outbound_router_delivers_error_frame_to_session_channel_chat(core, channel_env):
    """Router: reply_target frame wins, relation-map fallback for errors; DEDUPED stays silent."""
    env = channel_env
    relation_register.register_channel_chat(env.sid, "qq", "chat-A")
    router = core._ChannelOutboundRouter()

    # 1) Error frame: best-effort delivery via the live relation map.
    await router.send_error(env.sid, "boom")
    assert len(env.fake_channel.sent) == 1
    err = env.fake_channel.sent[0]
    assert err.chat_id == "chat-A"
    assert err.content == "boom"
    assert err.channel == "qq"

    # 2) Frame carrying reply_target JSON -> enqueue-time target wins (chat-B).
    await router.send(
        env.sid,
        {"reply_target": _reply_target("chat-B", "mid"), "content": "frame-reply"},
    )
    assert env.fake_channel.sent[1].chat_id == "chat-B"
    assert env.fake_channel.sent[1].metadata == {"message_id": "mid"}

    # 3) Frame without reply_target -> relation-map fallback (chat-A).
    await router.send(env.sid, {"content": "fallback"})
    assert env.fake_channel.sent[2].chat_id == "chat-A"

    # 4) DEDUPED branch: duplicate client_msg_id is silently ignored.
    env.detector["busy"] = True
    await env.store.enqueue(env.sid, _payload("first"), "user", client_msg_id="dup-1")
    message = SimpleNamespace(
        channel="qq",
        sender_id="user-4",
        chat_id="chat-A",
        content="dup",
        timestamp=0,
        media=None,
        metadata={"message_id": "dup-1"},
        session_id=None,
    )
    sent_before = len(env.fake_channel.sent)
    await core._process_inbound(message, env.fake_channel)

    rows = await env.store.list_active(env.sid)
    assert len(rows) == 1  # no second row
    assert env.generate_calls == []
    assert len(env.fake_channel.sent) == sent_before  # no new outbound
    assert env.stub_runner.finished == []


@pytest.mark.asyncio
async def test_channel_executor_resolves_own_claimed_row_when_queued_row_predates_dispatch(
    core, channel_env
):
    """F2 regression: executor must resolve its OWN CLAIMED placeholder.

    A QUEUED row may predate dispatch (input queued under hitl_pending /
    crash leftovers); list_active is FIFO by created_at, so rows[0] can be
    that older QUEUED row. Mirrors the WsTurnExecutor regression in
    tests/unit/runner/test_turn_runner.py (fix 32a5d2f).
    """
    env = channel_env
    relation_register.register_channel_chat(env.sid, "qq", "chat-A")

    older, _ = await env.store.enqueue(env.sid, _payload("queued-while-busy"), "user")
    placeholder = await env.store.insert_claimed(
        env.sid, _payload("fresh"), "user", reply_target=_reply_target("chat-B", None)
    )

    executor = iqs.get_default_registry().resolve("channel")
    assert executor is not None

    await executor.execute(env.sid, "fresh", "user", _reply_target("chat-B", None))
    await _wait_until(lambda: env.stub_runner.finished)

    assert env.generate_calls == [(env.sid, "fresh")], "the executor drives its own message"
    assert env.stub_runner.finished == [(env.sid, placeholder.id)], (
        "the executor's own CLAIMED placeholder must be resolved, not rows[0]"
    )
    rows = {row.id: row.status for row in await env.store.list_active(env.sid)}
    assert rows[older.id] == UserInputQueueStatus.QUEUED, (
        "the older QUEUED row belongs to the drain and must not be claimed here"
    )
