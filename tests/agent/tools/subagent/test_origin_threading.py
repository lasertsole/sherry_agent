"""Task 4 (subagent-origin-tagging) — origin threading through the server layer.

TDD tests for threading the subagent-completion carrier tag from the announce
pipeline into the graph input:

1. ``auto_turn._drive_turn`` extracts the injection's carrier metadata
   (frozen contract ``{internal: True, provenance: "subagent_completion",
   run_id, status}`` — completion_message.py:57-89, READ-ONLY) BEFORE
   flattening the injection into a ``MultiModalMessage`` and forwards it
   VERBATIM to ``async_generate`` as the new ``origin`` keyword.
2. ``async_generate(..., origin: dict | None = None)`` stamps ``origin``
   onto the ``HumanMessage`` it constructs for the graph (``metadata=origin``).
   The default (no origin) leaves the message without metadata, so the
   real-user call paths (server/trigger/ws/messages.py,
   server/trigger/channels/core.py) are behaviorally unchanged.
3. The user-takeover abandon path re-enqueues the ORIGINAL injection object
   (auto_turn ``_abandon_once`` → ``enqueue_steering``), so its carrier
   metadata survives for the consuming turn (Task 2 locked the rehydrate
   half of this).

Metadata flows through VERBATIM — no transformation, augmentation or
filtering anywhere on the path.
"""

import asyncio
import inspect

import pytest
from langchain_core.messages import HumanMessage
from type.message import MultiModalMessage

pytestmark = pytest.mark.unit


# Frozen carrier contract (completion_message.py:57-89) — read-only shape.
CARRIER_META = {
    "internal": True,
    "provenance": "subagent_completion",
    "run_id": "run-origin-1",
    "status": "completed",
}


def _at():
    from server.service import auto_turn

    return auto_turn


def _messages_mod():
    from server.service import messages

    return messages


def _sq():
    from agent.tools.subagent.announce import steering_queue

    return steering_queue


def _injection(run_id: str = "run-origin-1") -> HumanMessage:
    return HumanMessage(
        content=f"[subagent:{run_id}] child finished: done",
        metadata=dict(CARRIER_META, run_id=run_id),
    )


def _fake_detect(monkeypatch, at, state) -> None:
    from agent.tools.subagent.registry.session_state import SessionState

    def fake(session_key):
        return SessionState(session_id=session_key, busy=state["busy"], reason=state["reason"])

    monkeypatch.setattr(at, "detect_state", fake)


async def _no_interrupt(session_id):
    return None


def _empty_agen():
    """Async generator yielding nothing (mirrors an agent stream with no chunks)."""

    async def _gen():
        return
        yield  # pragma: no cover — presence makes this an async generator

    return _gen()


class _RecordingAgent:
    """Stand-in compiled graph: records the graph input, streams nothing."""

    def __init__(self, sink: dict):
        self._sink = sink

    def astream(self, *, input, config=None, stream_mode=None, **kwargs):
        self._sink["input"] = input
        return _empty_agen()


@pytest.fixture(autouse=True)
def _clean_state():
    """Isolate auto_turn inflight map + steering-queue singleton per test."""
    at = _at()
    sq = _sq()
    saved = dict(sq._QUEUE_HOLDER)
    at._INFLIGHT.clear()
    sq._QUEUE_HOLDER.clear()
    yield
    for task in list(at._INFLIGHT.values()):
        task.cancel()
    at._INFLIGHT.clear()
    sq._QUEUE_HOLDER.clear()
    sq._QUEUE_HOLDER.update(saved)


# ---------------------------------------------------------------------------
# (1) _drive_turn: metadata extracted BEFORE the MultiModalMessage flatten
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drive_turn_forwards_carrier_metadata_as_origin(monkeypatch):
    """_drive_turn passes the injection's carrier metadata as origin, verbatim."""
    at = _at()
    _fake_detect(monkeypatch, at, {"busy": False, "reason": "idle"})
    monkeypatch.setattr(at, "get_pending_interrupt", _no_interrupt)
    monkeypatch.setattr(at, "get_websocket_by_session_id", lambda sid: None)

    calls: list[dict] = []

    async def fake_generate(session_id, multi_modal_message, is_stream=True, origin=None):
        calls.append({"session_id": session_id, "origin": origin})
        yield {"type": "text", "content": "hello"}
        yield {"type": "meta", "model_name": "fake", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(at, "async_generate", fake_generate)

    injection = _injection()
    await at._drive_turn("sess-origin-1", injection)

    assert len(calls) == 1
    captured = calls[0]
    assert captured["session_id"] == "sess-origin-1"
    # Full contract forwarded — extracted BEFORE the flatten (the fake only
    # ever saw a MultiModalMessage, so the dict can only come from metadata).
    assert captured["origin"] == CARRIER_META
    assert captured["origin"]["provenance"] == "subagent_completion"
    assert captured["origin"]["internal"] is True
    assert captured["origin"]["run_id"] == "run-origin-1"
    assert captured["origin"]["status"] == "completed"
    # Verbatim passthrough: the exact metadata object, not a copy/transform.
    assert captured["origin"] is getattr(injection, "metadata", None)


# ---------------------------------------------------------------------------
# (2) async_generate default: real-user paths unchanged (no metadata stamp)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_generate_without_origin_keeps_humanmessage_metadata_empty(monkeypatch):
    """async_generate WITHOUT origin (WS/channels default): HumanMessage has no/None metadata."""
    mm = _messages_mod()
    sink: dict = {}

    async def fake_built_agent(**kwargs):
        return _RecordingAgent(sink)

    monkeypatch.setattr(mm, "built_agent", fake_built_agent)

    frames = []
    async for frame in mm.async_generate("sess-origin-2", MultiModalMessage(text="hi")):
        frames.append(frame)

    assert any(f.get("type") == "meta" for f in frames)  # turn completed normally
    hm = sink["input"]["messages"][0]
    assert isinstance(hm, HumanMessage)
    # getattr-defensive: pre-Task-4 messages do not even carry the attribute;
    # post-Task-4 the default origin=None stamps metadata=None. Both are falsy.
    assert not getattr(hm, "metadata", None)


def test_async_generate_signature_has_origin_kwarg_default_none():
    """origin is a keyword parameter with default None; positional order unchanged."""
    mm = _messages_mod()
    params = inspect.signature(mm.async_generate).parameters
    assert "origin" in params
    assert params["origin"].default is None
    assert params["origin"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    positional = [
        p.name
        for p in params.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    assert positional == ["session_id", "multi_modal_message", "is_stream", "origin"]


# ---------------------------------------------------------------------------
# (3) async_generate with origin: HumanMessage stamped verbatim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_generate_stamps_origin_onto_humanmessage(monkeypatch):
    """async_generate(origin=...) → constructed HumanMessage.metadata == origin."""
    mm = _messages_mod()
    sink: dict = {}

    async def fake_built_agent(**kwargs):
        return _RecordingAgent(sink)

    monkeypatch.setattr(mm, "built_agent", fake_built_agent)

    origin = {
        "internal": True,
        "provenance": "subagent_completion",
        "run_id": "run-origin-3",
        "status": "completed",
    }
    frames = []
    async for frame in mm.async_generate(
        "sess-origin-3", MultiModalMessage(text="hi"), is_stream=True, origin=origin
    ):
        frames.append(frame)

    assert any(f.get("type") == "meta" for f in frames)
    hm = sink["input"]["messages"][0]
    assert isinstance(hm, HumanMessage)
    stamped = getattr(hm, "metadata", None) or {}
    assert stamped == origin
    assert stamped["internal"] is True
    assert stamped["provenance"] == "subagent_completion"
    assert stamped["run_id"] == "run-origin-3"
    assert stamped["status"] == "completed"


# ---------------------------------------------------------------------------
# (4) User input mid-turn under the NEW queueing semantics (Task 8): the auto
#     turn is NOT cancelled and NOT re-queued; the original injection keeps
#     its carrier metadata on the completed turn's async_generate call.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_takeover_no_requeue_turn_completes(monkeypatch, tmp_path):
    """User input mid-turn no longer abandons the turn (Task 8): no steering
    requeue, no PENDING mirror; origin metadata still forwarded verbatim."""
    from agent.tools.subagent.announce.steering_queue import SteeringQueue
    from agent.tools.subagent.registry.pending_injections import PendingInjectionStore

    at = _at()
    sq = _sq()
    store = PendingInjectionStore(db_path=tmp_path / "origin.db")
    sq._QUEUE_HOLDER["queue"] = SteeringQueue(store=store)  # real seam, tmp DB
    _fake_detect(monkeypatch, at, {"busy": False, "reason": "idle"})
    monkeypatch.setattr(at, "get_pending_interrupt", _no_interrupt)
    monkeypatch.setattr(at, "get_websocket_by_session_id", lambda sid: None)

    calls: list[dict] = []
    started = asyncio.Event()
    block = asyncio.Event()
    finished = asyncio.Event()

    async def fake_generate(session_id, multi_modal_message, is_stream=True, origin=None):
        calls.append({"session_id": session_id, "origin": origin})
        started.set()
        try:
            await asyncio.wait_for(block.wait(), timeout=10)
        except asyncio.CancelledError:
            raise
        yield {"type": "text", "content": "hello"}
        yield {"type": "meta", "model_name": "fake", "input_tokens": 1, "output_tokens": 1}
        finished.set()

    monkeypatch.setattr(at, "async_generate", fake_generate)

    injection = _injection("run-origin-5")
    result = await at.maybe_trigger_auto_turn("sess-origin-5", injection)
    assert result.outcome == at.AutoTurnOutcome.TRIGGERED
    for _ in range(200):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set(), "fake generate never started"

    # User frame arrives mid-turn: detector flips to ws_task — nothing cancels.
    _fake_detect(monkeypatch, at, {"busy": True, "reason": "ws_task"})
    await asyncio.sleep(0.1)
    assert not at._INFLIGHT["sess-origin-5"].done(), "auto turn was cancelled by user input"

    block.set()  # user input queues; the auto turn runs to completion
    await asyncio.wait_for(at._INFLIGHT["sess-origin-5"], timeout=10)
    assert finished.is_set()

    # The turn consumed the injection itself: origin metadata forwarded verbatim.
    assert len(calls) == 1
    assert calls[0]["session_id"] == "sess-origin-5"
    assert calls[0]["origin"] is injection.metadata

    # No takeover abandon: steering queue stays empty, durable mirror absent.
    queue = sq.get_steering_queue()
    assert await queue.rehydrate("sess-origin-5") == []
    assert await store.get("run-origin-5") is None
