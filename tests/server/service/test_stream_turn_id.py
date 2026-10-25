"""Turn identity on every stream frame + the history endpoint's snapshot.

The reconnect contract now has two halves: every frame names its turn, and the
history read can return the running turn's snapshot in the same response. These
tests pin the wire fields (no real sockets / model calls).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from server.service.stream_dispatch import StreamTurn
from server.service.stream_driver import StreamDriver
from server.service.trajectory_store import turn_scope

pytestmark = [pytest.mark.unit]


class _RecordingDriver(StreamDriver):
    def __init__(self, turn_info: dict[str, Any] | None = None, turn_id: str = "") -> None:
        super().__init__("sess-turn-id", object(), turn_info, turn_id)
        self.frames: list[dict[str, Any]] = []

    async def send_frame(self, payload: dict[str, Any]) -> None:
        self.frames.append(payload)

    async def check_interrupt(self) -> dict[str, Any] | None:
        return None

    async def on_finish(self) -> None:
        return None


async def _empty() -> AsyncGenerator[dict[str, str]]:
    return
    yield  # pragma: no cover


def test_driver_frames_carry_the_generation_turn_id():
    driver = _RecordingDriver({"turn_id": "turn-a", "message_ids": ["m1"]})

    asyncio.run(driver.drive(_empty()))

    assert [frame["event"] for frame in driver.frames] == ["turn_started", "done"]
    assert all(frame["turn_id"] == "turn-a" for frame in driver.frames)


def test_driver_accepts_an_explicit_turn_id_without_turn_info():
    driver = _RecordingDriver(turn_id="turn-b")

    asyncio.run(driver.drive(_empty()))

    # turn_info is absent (resume/auto-turn): no turn_started, but done still
    # names the turn.
    assert [frame["event"] for frame in driver.frames] == ["done"]
    assert driver.frames[0]["turn_id"] == "turn-b"


class _OneFrameTurn(StreamTurn):
    def __init__(self) -> None:
        super().__init__("sess-turn-id")

    async def _run_frames(self) -> AsyncGenerator[dict[str, Any]]:
        yield {"type": "chunk", "content": "hello"}


def test_stream_turn_stamps_turn_id_and_seq_together():
    async def collect() -> list[dict[str, Any]]:
        with turn_scope("turn-c", "sess-turn-id"):
            turn = _OneFrameTurn()
        frames = [frame async for frame in turn.run()]
        return frames

    frames = asyncio.run(collect())

    assert frames == [{"type": "chunk", "content": "hello", "seq": 1, "turn_id": "turn-c"}]
