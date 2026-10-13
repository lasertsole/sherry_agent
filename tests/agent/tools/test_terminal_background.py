"""Background terminal tasks: the spawn contract, the registry, the two tools.

``terminal(run_in_background=True)`` returns immediately with a task id while
the child keeps running; its output goes to a RETAINED file (readable while it
runs and after it exits), a watcher records the exit and notifies, and the two
query tools let the agent ask about it later. Everything is fail-open: a lost
notification must never lose the task.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from agent.tools.pub_base import read_state
from agent.tools.terminal import build_terminal_tool
from agent.tools.terminal_bg_registry import (
    BackgroundTask,
    BackgroundTaskRegistry,
    background_registry,
    open_background_output,
    read_output_tail,
)
from agent.tools.terminal_bg_tools import terminal_bg_list, terminal_bg_output

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "s-bg-task"


@pytest.fixture
def sessions_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import config

    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(config, "SESSIONS_DIR", root)
    import pub.func.message.eviction as eviction_mod

    monkeypatch.setattr(eviction_mod, "SESSIONS_DIR", root)
    read_state.forget_all()
    for task in background_registry.list_session(SESSION):
        background_registry.update(task.task_id, status="killed")
    yield root


class TestRetainedOutput:
    def test_the_output_file_lives_under_the_session_evicted_dir(self, sessions_root: Path):
        path = open_background_output(SESSION, "bg-abc")

        assert path is not None and path.exists()
        assert path.parent.name == "evicted"
        assert path.name == "terminal-bg-bg-abc.log"

    def test_an_unsafe_session_id_yields_none(self, sessions_root: Path):
        assert open_background_output("../evil", "bg-abc") is None

    def test_the_tail_read_is_bounded_and_labelled(self, tmp_path: Path):
        path = tmp_path / "out.log"
        path.write_text("a" * 100 + "TAIL", encoding="utf-8")

        text = read_output_tail(path, 8)

        assert text.endswith("TAIL")
        assert "[showing the last 8 of 104 bytes]" in text

    def test_a_small_file_is_returned_whole(self, tmp_path: Path):
        path = tmp_path / "out.log"
        path.write_text("short output", encoding="utf-8")

        assert read_output_tail(path, 8192) == "short output"

    def test_a_missing_file_is_empty(self, tmp_path: Path):
        assert read_output_tail(tmp_path / "gone.log") == ""


class TestRegistry:
    def test_the_registry_tracks_status_and_the_running_count(self, sessions_root: Path):
        registry = BackgroundTaskRegistry()
        task = BackgroundTask(
            task_id="bg-1",
            session_id=SESSION,
            pid=123,
            command="sleep 1",
            output_path=None,
            started_at=time.monotonic(),
        )
        registry.register(task)

        assert registry.running_count(SESSION) == 1
        assert registry.get("bg-1") is task

        registry.update("bg-1", status="completed", exit_code=0)

        assert registry.running_count(SESSION) == 0
        assert task.exit_code == 0 and task.live is False

    def test_list_session_is_newest_first(self, sessions_root: Path):
        registry = BackgroundTaskRegistry()
        for index in (1, 2, 3):
            registry.register(
                BackgroundTask(
                    task_id=f"bg-{index}",
                    session_id=SESSION,
                    pid=index,
                    command=f"cmd {index}",
                    output_path=None,
                    started_at=float(index),
                )
            )

        assert [t.task_id for t in registry.list_session(SESSION)] == ["bg-3", "bg-2", "bg-1"]

    def test_kill_session_tasks_signals_and_marks(self, sessions_root: Path, monkeypatch):
        registry = BackgroundTaskRegistry()
        killed: list[int] = []
        monkeypatch.setattr("os.kill", lambda pid, sig: killed.append(pid), raising=True)
        registry.register(
            BackgroundTask(
                task_id="bg-live",
                session_id=SESSION,
                pid=4242,
                command="sleep 99",
                output_path=None,
                started_at=time.monotonic(),
            )
        )
        registry.register(
            BackgroundTask(
                task_id="bg-done",
                session_id=SESSION,
                pid=4243,
                command="true",
                output_path=None,
                started_at=time.monotonic(),
                status="completed",
            )
        )

        assert registry.kill_session_tasks(SESSION) == 1
        assert killed == [4242]
        assert registry.get("bg-live").status == "killed"
        assert registry.get("bg-done").status == "completed", "a finished task is left alone"


class TestWatcher:
    @pytest.mark.asyncio
    async def test_the_watcher_records_the_exit_and_notifies(
        self, sessions_root: Path, monkeypatch
    ):
        registry = BackgroundTaskRegistry()
        pushed: list[dict] = []
        turned: list[str] = []

        class _Socket:
            async def send_text(self, payload: str) -> None:
                pushed.append(json.loads(payload))

        import importlib

        # The module and the manager SHARE the name; import by path so the
        # module (not the instance re-exported by the package) is patched.
        relation_mod = importlib.import_module("runtime.session.relation_register")
        monkeypatch.setattr(
            relation_mod.relation_register,
            "get_websocket_by_session_id",
            lambda session_id: _Socket(),
        )
        hooks_mod = importlib.import_module("runtime.hooks")

        async def _trigger(session_key: str, message) -> None:
            turned.append(message.content)

        monkeypatch.setattr(hooks_mod, "resolve", lambda name: _trigger)

        task = BackgroundTask(
            task_id="bg-w",
            session_id=SESSION,
            pid=1,
            command="echo hi",
            output_path=None,
            started_at=time.monotonic(),
        )
        registry.register(task)

        class _Proc:
            returncode = 0

            async def wait(self) -> int:
                return 0

        registry.start_watcher("bg-w", _Proc(), SESSION)
        for _ in range(50):
            if task.status != "running":
                break
            await asyncio.sleep(0.02)

        assert task.status == "completed" and task.exit_code == 0
        assert pushed and pushed[0]["event"] == "terminal_bg_exit"
        assert pushed[0]["content"]["task_id"] == "bg-w"
        assert turned and "exited with code 0" in turned[0]

    @pytest.mark.asyncio
    async def test_a_failing_exit_is_reported_as_failed(self, sessions_root: Path):
        registry = BackgroundTaskRegistry()
        task = BackgroundTask(
            task_id="bg-f",
            session_id=SESSION,
            pid=1,
            command="false",
            output_path=None,
            started_at=time.monotonic(),
        )
        registry.register(task)

        class _Proc:
            returncode = 3

            async def wait(self) -> int:
                return 3

        registry.start_watcher("bg-f", _Proc(), SESSION)
        for _ in range(50):
            if task.status != "running":
                break
            await asyncio.sleep(0.02)

        assert task.status == "failed" and task.exit_code == 3


class TestQueryTools:
    def test_the_list_is_json_and_empty_without_tasks(self, sessions_root: Path):
        assert "No background terminal tasks" in terminal_bg_list.invoke({"session_id": SESSION})

    def test_the_list_reports_the_task_fields(self, sessions_root: Path):
        background_registry.register(
            BackgroundTask(
                task_id="bg-list",
                session_id=SESSION,
                pid=77,
                command="serve",
                output_path=None,
                started_at=time.monotonic(),
            )
        )

        rows = json.loads(terminal_bg_list.invoke({"session_id": SESSION}))

        assert rows[0]["task_id"] == "bg-list"
        assert rows[0]["status"] == "running"
        assert rows[0]["pid"] == 77

    def test_another_sessions_task_is_not_readable(self, sessions_root: Path):
        background_registry.register(
            BackgroundTask(
                task_id="bg-other",
                session_id="someone-else",
                pid=1,
                command="x",
                output_path=None,
                started_at=time.monotonic(),
            )
        )

        assert "No background task found" in terminal_bg_output.invoke(
            {"task_id": "bg-other", "session_id": SESSION}
        )

    def test_the_output_tool_reads_the_retained_file(self, sessions_root: Path):
        path = open_background_output(SESSION, "bg-read")
        assert path is not None
        path.write_text("compiling...\ndone\n", encoding="utf-8")
        background_registry.register(
            BackgroundTask(
                task_id="bg-read",
                session_id=SESSION,
                pid=1,
                command="build",
                output_path=path,
                started_at=time.monotonic(),
                status="completed",
                exit_code=0,
            )
        )

        assert terminal_bg_output.invoke({"task_id": "bg-read", "session_id": SESSION}) == (
            "compiling...\ndone\n"
        )

    def test_an_unknown_task_says_so(self, sessions_root: Path):
        assert "No background task found" in terminal_bg_output.invoke(
            {"task_id": "bg-nope", "session_id": SESSION}
        )


class TestSpawn:
    @pytest.mark.timeout(30)
    def test_a_background_command_returns_immediately_and_completes(self, sessions_root: Path):
        # One loop for the whole scenario: the watcher is a task on the loop
        # that spawned it (in production that is the server's long-lived loop),
        # so closing the loop would kill the watcher before it observes the exit.
        tool = build_terminal_tool()

        async def _run() -> tuple[str, float, str]:
            started = time.monotonic()
            # Through the TOOL INPUT: langchain injects schema fields by NAME,
            # so this is the only call shape that proves `run_in_background`
            # reaches the spawn path (a renamed parameter silently falls into
            # **kwargs and the command runs in the foreground).
            out = await tool._arun(
                "sleep 0.6; echo bg-done",
                session_id=SESSION,
                run_in_background=True,
            )
            elapsed = time.monotonic() - started
            task_id = out.split("Task ID: ", 1)[1].split("\n", 1)[0]
            for _ in range(200):
                if background_registry.get(task_id).status != "running":
                    break
                await asyncio.sleep(0.05)
            return out, elapsed, task_id

        out, elapsed, task_id = asyncio.run(_run())

        assert "Background task started." in out and task_id.startswith("bg-")
        assert elapsed < 3.0, "the tool must not wait for the command"
        task = background_registry.get(task_id)
        assert task is not None and task.output_path is not None
        assert task.status == "completed" and task.exit_code == 0
        assert "bg-done" in task.output_path.read_text(encoding="utf-8")

    @pytest.mark.timeout(30)
    def test_the_session_cap_refuses_a_further_spawn(self, sessions_root: Path):
        from config.features import TERMINAL_BG

        tool = build_terminal_tool()
        for index in range(TERMINAL_BG["max_tasks_per_session"]):
            background_registry.register(
                BackgroundTask(
                    task_id=f"bg-cap-{index}",
                    session_id=SESSION,
                    pid=1,
                    command="sleep 99",
                    output_path=None,
                    started_at=time.monotonic(),
                )
            )

        out = asyncio.run(tool._arun("echo nope", session_id=SESSION, run_in_background=True))

        assert "background task(s) running" in out
        assert "limit" in out

    @pytest.mark.timeout(30)
    def test_an_unsafe_session_is_refused_without_spawning(self, sessions_root: Path):
        tool = build_terminal_tool()

        out = asyncio.run(tool._arun("echo nope", session_id="../evil", run_in_background=True))

        assert "invalid session" in out


class TestSchemaWiring:
    def test_the_schema_field_name_matches_the_spawn_parameter(self):
        """langchain injects schema fields BY NAME — the two must agree.

        A renamed parameter silently lands in ``**kwargs`` and the command runs
        in the foreground (found live: `run_in_background` never reached the
        background branch).
        """
        import inspect

        from agent.tools.terminal import SafeShellInput, SafeShellTool

        assert "run_in_background" in SafeShellInput.model_fields
        parameters = inspect.signature(SafeShellTool._arun).parameters
        assert "run_in_background" in parameters
        assert "run_background" not in parameters, "the name must match the schema field"

    def test_invoking_through_the_tool_input_starts_a_background_task(self, sessions_root: Path):
        """The production call shape (schema validation → kwargs) is exercised."""
        import asyncio

        tool = build_terminal_tool()
        payload = tool.input_schema.model_validate(
            {"commands": "echo via-input; sleep 0.3", "run_in_background": True}
        )
        out = asyncio.run(
            tool._arun(
                payload.commands,
                session_id=SESSION,
                run_in_background=payload.run_in_background,
            )
        )

        assert "Background task started." in out
