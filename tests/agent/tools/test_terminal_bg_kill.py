"""Command-level cancel for background terminal tasks.

``terminal(run_in_background=True)`` starts a child that outlives the turn; this
suite pins the SECOND half of that contract — stopping ONE command without
touching the turn, the other commands, or the session:

* ``BackgroundTaskRegistry.kill_task`` reuses the session-delete tree kill,
  scoped to one id, and refuses an unknown/foreign/finished task (F5/F6);
* the watcher must NOT rewrite "killed" as "failed" when the process it killed
  finally reports its (non-zero) exit code — otherwise the panel flickers
  killed → failed;
* the exit notification and the auto-turn text tell "stopped by a person" apart
  from "the command failed";
* ``terminal_bg(action="kill")`` is the agent's own path to the same method.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import time

import pytest

from agent.tools.terminal.bg_registry import BackgroundTask, BackgroundTaskRegistry
from agent.tools.terminal.bg_tools import terminal_bg

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "s-bg-kill"
OTHER_SESSION = "s-bg-other"


@pytest.fixture
def kill_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int, float]]:
    """Record every tree kill instead of signalling a real process."""
    import agent.tools.pub_base.process_reap as reap

    calls: list[tuple[int, int, float]] = []

    def _kill(proc, *, pgid=None, grace=1.0):
        calls.append((proc.pid, pgid, grace))

    monkeypatch.setattr(reap, "kill_process_tree", _kill)
    return calls


def _task(
    task_id: str = "bg-1",
    *,
    session: str = SESSION,
    status: str = "running",
    pid: int | None = 4242,
) -> BackgroundTask:
    return BackgroundTask(
        task_id=task_id,
        session_id=session,
        pid=pid,
        command="sleep 1000",
        output_path=None,
        started_at=time.monotonic(),
        status=status,
    )


class TestKillTask:
    def test_a_running_task_is_tree_killed_and_marked_killed(self, kill_calls):
        registry = BackgroundTaskRegistry()
        registry.register(_task())

        assert registry.kill_task("bg-1", SESSION) is True

        assert kill_calls == [(4242, 4242, 1.0)]
        task = registry.get("bg-1")
        assert task is not None and task.status == "killed"

    def test_the_recorded_process_group_is_used(self, kill_calls):
        registry = BackgroundTaskRegistry()
        task = _task()
        task.pgid = 9000
        registry.register(task)

        registry.kill_task("bg-1", SESSION)

        assert kill_calls == [(4242, 9000, 1.0)]

    def test_an_unknown_id_is_refused_without_signalling(self, kill_calls):
        registry = BackgroundTaskRegistry()

        assert registry.kill_task("nope", SESSION) is False
        assert kill_calls == []

    def test_another_sessions_task_is_refused(self, kill_calls):
        """F6: the task id is bound to its session."""
        registry = BackgroundTaskRegistry()
        registry.register(_task())

        assert registry.kill_task("bg-1", OTHER_SESSION) is False
        assert kill_calls == []
        task = registry.get("bg-1")
        assert task is not None and task.status == "running"

    def test_a_finished_task_is_refused(self, kill_calls):
        registry = BackgroundTaskRegistry()
        registry.register(_task(status="completed"))

        assert registry.kill_task("bg-1", SESSION) is False
        assert kill_calls == []

    def test_a_task_without_a_pid_is_refused(self, kill_calls):
        registry = BackgroundTaskRegistry()
        registry.register(_task(pid=None))

        assert registry.kill_task("bg-1", SESSION) is False
        assert kill_calls == []

    def test_an_already_dead_process_still_marks_killed(self, monkeypatch: pytest.MonkeyPatch):
        """The signal can race the exit; the status must still settle."""
        import agent.tools.pub_base.process_reap as reap

        def _gone(proc, *, pgid=None, grace=1.0):
            raise ProcessLookupError

        monkeypatch.setattr(reap, "kill_process_tree", _gone)
        registry = BackgroundTaskRegistry()
        registry.register(_task())

        assert registry.kill_task("bg-1", SESSION) is True
        task = registry.get("bg-1")
        assert task is not None and task.status == "killed"


class TestWatcherAfterKill:
    @pytest.mark.asyncio
    async def test_the_watcher_keeps_killed_and_records_the_exit_code(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        registry = BackgroundTaskRegistry()
        task = _task()
        registry.register(task)

        class _Proc:
            returncode = -15

            async def wait(self) -> int:
                return -15

        # Kill first (as the user would), THEN let the watcher observe the exit.
        registry.update("bg-1", status="killed")
        registry.start_watcher("bg-1", _Proc(), SESSION)
        for _ in range(50):
            if task.exit_code is not None:
                break
            await asyncio.sleep(0.02)

        assert task.status == "killed", "the watcher must not turn a kill into 'failed'"
        assert task.exit_code == -15

    @pytest.mark.asyncio
    async def test_the_exit_frame_and_auto_turn_say_killed(self, monkeypatch: pytest.MonkeyPatch):
        registry = BackgroundTaskRegistry()
        pushed: list[dict] = []
        turned: list[str] = []

        class _Socket:
            async def send_text(self, payload: str) -> None:
                pushed.append(json.loads(payload))

        relation_mod = importlib.import_module("runtime.session.relation_register")
        monkeypatch.setattr(
            relation_mod.relation_register,
            "get_websockets_by_session_id",
            lambda session_id: [_Socket()],
        )
        hooks_mod = importlib.import_module("runtime.hooks")

        async def _trigger(session_key: str, message) -> None:
            turned.append(message.content)

        monkeypatch.setattr(hooks_mod, "resolve", lambda name: _trigger)

        task = _task()
        registry.register(task)

        class _Proc:
            returncode = -15

            async def wait(self) -> int:
                return -15

        registry.update("bg-1", status="killed")
        registry.start_watcher("bg-1", _Proc(), SESSION)
        for _ in range(50):
            if turned:
                break
            await asyncio.sleep(0.02)

        assert pushed and pushed[0]["event"] == "terminal_bg_exit"
        content = pushed[0]["content"]
        assert content["status"] == "killed"
        assert content["reason"] == "killed"
        assert turned and "stopped by user" in turned[0]
        assert "FAILED" not in turned[0]

    @pytest.mark.asyncio
    async def test_a_natural_failure_still_reads_failed(self, monkeypatch: pytest.MonkeyPatch):
        registry = BackgroundTaskRegistry()
        pushed: list[dict] = []

        class _Socket:
            async def send_text(self, payload: str) -> None:
                pushed.append(json.loads(payload))

        relation_mod = importlib.import_module("runtime.session.relation_register")
        monkeypatch.setattr(
            relation_mod.relation_register,
            "get_websockets_by_session_id",
            lambda session_id: [_Socket()],
        )
        hooks_mod = importlib.import_module("runtime.hooks")
        monkeypatch.setattr(hooks_mod, "resolve", lambda name: None)  # no auto-turn owner

        task = _task()
        registry.register(task)

        class _Proc:
            returncode = 2

            async def wait(self) -> int:
                return 2

        registry.start_watcher("bg-1", _Proc(), SESSION)
        for _ in range(50):
            if pushed:
                break
            await asyncio.sleep(0.02)

        assert task.status == "failed"
        assert pushed[0]["content"]["status"] == "failed"
        assert pushed[0]["content"]["reason"] == "exited"


class TestStartedFrame:
    @pytest.mark.asyncio
    async def test_a_started_frame_carries_the_task_record(self, monkeypatch: pytest.MonkeyPatch):
        registry = BackgroundTaskRegistry()
        pushed: list[dict] = []

        class _Socket:
            async def send_text(self, payload: str) -> None:
                pushed.append(json.loads(payload))

        relation_mod = importlib.import_module("runtime.session.relation_register")
        monkeypatch.setattr(
            relation_mod.relation_register,
            "get_websockets_by_session_id",
            lambda session_id: [_Socket()],
        )

        task = _task()
        registry.register(task)
        await registry.push_started(task)

        assert pushed and pushed[0]["event"] == "terminal_bg_started"
        assert pushed[0]["content"]["task_id"] == "bg-1"
        assert pushed[0]["content"]["status"] == "running"


class TestKillToolAction:
    def test_the_agent_can_stop_a_running_task(self, kill_calls):
        from agent.tools.terminal.bg_registry import background_registry

        background_registry.register(_task("bg-tool"))
        try:
            out = terminal_bg.invoke(
                {"action": "kill", "task_id": "bg-tool", "session_id": SESSION}
            )
        finally:
            background_registry.update("bg-tool", status="killed")

        assert "stopped" in out
        assert background_registry.get("bg-tool").status == "killed"

    def test_a_missing_task_id_is_a_usage_error(self):
        out = terminal_bg.invoke({"action": "kill", "session_id": SESSION})

        assert out.startswith("Error:") and "task_id" in out

    def test_another_sessions_task_is_not_found(self):
        from agent.tools.terminal.bg_registry import background_registry

        background_registry.register(_task("bg-foreign"))
        try:
            out = terminal_bg.invoke(
                {"action": "kill", "task_id": "bg-foreign", "session_id": OTHER_SESSION}
            )
        finally:
            background_registry.update("bg-foreign", status="killed")

        assert "No background task found" in out

    def test_a_finished_task_says_there_is_nothing_to_stop(self):
        from agent.tools.terminal.bg_registry import background_registry

        background_registry.register(_task("bg-done", status="completed"))
        try:
            out = terminal_bg.invoke(
                {"action": "kill", "task_id": "bg-done", "session_id": SESSION}
            )
        finally:
            background_registry.update("bg-done", status="killed")

        assert "not running" in out

    def test_the_unknown_action_message_lists_kill(self):
        out = terminal_bg.invoke({"action": "explode", "session_id": SESSION})

        assert "list, output, kill" in out
