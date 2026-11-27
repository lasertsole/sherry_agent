"""Regression: a cancelled turn must be visible — and must invalidate the run.

Measured on the DeepSWE L1 rerun of 2026-11-27: pest's turn was cancelled at
981.8 s. The driver's ``stopped`` frame (content ``Request cancelled``, no
``done``) was consumed as ordinary text, so the run looked like a completed turn
that simply produced nothing and was graded as a normal failure
(``failure_kind=""``). ``error`` frames were dropped the same way.
"""

from __future__ import annotations

from typing import Any

import pytest

from evals.coding_capability import suite as harness
from evals.deepswe import suite as deepswe

pytestmark = [pytest.mark.regression, pytest.mark.timeout(30)]


async def _drive_with_frames(monkeypatch: pytest.MonkeyPatch, frames: list[dict[str, Any]]):
    import agent.state_port
    import server.service.messages as messages

    async def fake_generate(
        session_id: str,
        message: Any,
        is_stream: bool = True,  # noqa: ARG001
    ):
        for frame in frames:
            yield frame

    async def no_interrupt(session_id: str) -> None:  # noqa: ARG001
        return None

    monkeypatch.setattr(messages, "async_generate", fake_generate)
    monkeypatch.setattr(agent.state_port, "read_interrupt", no_interrupt)
    return await harness._drive("eval-session", "do the task")


@pytest.mark.asyncio
async def test_a_stopped_frame_marks_the_drive_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drive = await _drive_with_frames(
        monkeypatch,
        [
            {"event": "tool_start", "content": "terminal"},
            {"event": "text", "content": "Request cancelled"},
            {"event": "stopped", "content": "Request cancelled"},
        ],
    )

    assert drive["cancelled"] is True
    assert drive["tool_calls"] == 1
    assert harness._failure_kind(False, drive) == "cancelled"


@pytest.mark.asyncio
async def test_an_error_frame_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    drive = await _drive_with_frames(
        monkeypatch,
        [{"event": "error", "content": "APIStatusError: Error code: 402 - no balance"}],
    )

    assert drive["error"].startswith("APIStatusError")
    assert harness._failure_kind(False, drive) == "provider"


def test_invalid_kinds_cover_provider_cancel_and_harness() -> None:
    assert deepswe._INVALID_FAILURE_KINDS == {"provider", "cancelled", "harness"}


def test_timeout_and_a_plain_miss_stay_real_outcomes() -> None:
    timed_out: dict[str, Any] = {"timeout": True, "cancelled": False, "error": ""}
    completed: dict[str, Any] = {"timeout": False, "cancelled": False, "error": ""}

    assert harness._failure_kind(False, timed_out) == "timeout"
    assert harness._failure_kind(False, completed) == ""
    assert "timeout" not in deepswe._INVALID_FAILURE_KINDS
