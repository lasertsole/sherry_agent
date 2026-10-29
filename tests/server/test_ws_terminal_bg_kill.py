"""The ``terminal_bg_kill`` WS frame: a command-level stop from the UI.

The frame is the panel's kill button. It must answer with an acknowledgement
(the signal landed, or why it did not) and never touch the running turn; the
registry's watcher still sends ``terminal_bg_exit`` with the final state.
"""

from __future__ import annotations

import time

import pytest

from agent.tools.terminal_bg_registry import BackgroundTask, background_registry
from server.trigger.ws.messages import _handle_terminal_bg_kill

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "s-ws-bg"
OTHER_SESSION = "s-ws-other"


class _Socket:
    """Minimal websocket adapter: records what the handler sends."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, payload: str) -> None:
        import json

        self.sent.append(json.loads(payload))


@pytest.fixture
def running_task(monkeypatch: pytest.MonkeyPatch) -> BackgroundTask:
    import agent.tools.pub_base.process_reap as reap

    monkeypatch.setattr(reap, "kill_process_tree", lambda *args, **kwargs: None)
    task = BackgroundTask(
        task_id="bg-ws",
        session_id=SESSION,
        pid=999_999,
        command="sleep 1000",
        output_path=None,
        started_at=time.monotonic(),
    )
    background_registry.register(task)
    yield task
    background_registry.update(task.task_id, status="killed")


@pytest.mark.asyncio
async def test_a_running_task_is_killed_and_acknowledged(running_task: BackgroundTask):
    socket = _Socket()

    await _handle_terminal_bg_kill(socket, SESSION, {"task_id": "bg-ws"})

    assert socket.sent and socket.sent[0]["event"] == "terminal_bg_kill_ack"
    assert socket.sent[0]["content"]["ok"] is True
    assert background_registry.get("bg-ws").status == "killed"


@pytest.mark.asyncio
async def test_another_sessions_task_is_refused(running_task: BackgroundTask):
    socket = _Socket()

    await _handle_terminal_bg_kill(socket, OTHER_SESSION, {"task_id": "bg-ws"})

    assert socket.sent[0]["content"]["ok"] is False
    assert socket.sent[0]["content"]["reason"] == "not_found_or_not_running"
    assert background_registry.get("bg-ws").status == "running"


@pytest.mark.asyncio
async def test_a_missing_task_id_is_refused():
    socket = _Socket()

    await _handle_terminal_bg_kill(socket, SESSION, {})

    assert socket.sent[0]["content"] == {
        "ok": False,
        "task_id": "",
        "reason": "missing task_id",
    }
