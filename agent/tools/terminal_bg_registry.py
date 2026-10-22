"""Process-level registry for background terminal tasks.

``terminal(run_in_background=True)`` must return IMMEDIATELY while the child
keeps running, and the agent must be able to ask about it later in the session —
two facts this module owns:

* the task record (id, pid, command, output file, exit status), and
* a watcher that awaits the child's exit and then tells the world: a WS frame
  (``terminal_bg_exit``) for the UI and an auto-turn for the agent.

In-memory and process-level on purpose: the tasks are CHILDREN of this process,
so a server restart leaves nothing worth persisting (the OS reaps orphaned
children); state is scoped per session and capped by ``TERMINAL_BG``. Every
notification step is fail-open — losing a notice must never lose the task.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from config.features import TERMINAL_BG
from pub.func.message.eviction import get_eviction_dir

__all__ = ["BackgroundTask", "BackgroundTaskRegistry", "background_registry"]


@dataclass
class BackgroundTask:
    """One backgrounded terminal command."""

    task_id: str
    session_id: str
    pid: int | None
    command: str
    output_path: Path | None
    started_at: float  # time.monotonic()
    pgid: int | None = None  # process-group id recorded at spawn (tree cleanup)
    status: str = "running"  # running | completed | failed | killed
    exit_code: int | None = None
    live: bool = False  # whether a watcher still holds the process handle

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape the query tools and the WS frame share."""
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "pid": self.pid,
            "command": self.command,
            "output_path": str(self.output_path) if self.output_path else None,
            "status": self.status,
            "exit_code": self.exit_code,
        }


class BackgroundTaskRegistry:
    """The registry + the watcher ledger (one instance, ``background_registry``)."""

    def __init__(self) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        # Strong references: an asyncio.Task is garbage-collected the moment
        # nothing holds it, and a collected watcher would silently stop watching
        # (the same pattern subagent/announce/core.py uses).
        self._watchers: set[asyncio.Task[None]] = set()

    # ── queries ──────────────────────────────────────────────────────────────

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def list_session(self, session_id: str) -> list[BackgroundTask]:
        """This session's tasks, newest first."""
        return sorted(
            (task for task in self._tasks.values() if task.session_id == session_id),
            key=lambda task: task.started_at,
            reverse=True,
        )

    def running_count(self, session_id: str) -> int:
        return sum(
            1
            for task in self._tasks.values()
            if task.session_id == session_id and task.status == "running"
        )

    # ── registration ─────────────────────────────────────────────────────────

    def register(self, task: BackgroundTask) -> None:
        self._tasks[task.task_id] = task

    def update(self, task_id: str, *, status: str, exit_code: int | None = None) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.status = status
        task.exit_code = exit_code
        task.live = False

    def kill_session_tasks(self, session_id: str) -> int:
        """Kill the process TREE of every still-running task of one session.

        Session deletion is the one place a background task is not preserved.
        The group recorded at spawn is killed (SIGTERM → bounded SIGKILL), so a
        background shell's children do not survive the cleanup. Best-effort by
        design; the session directory is removed right after by the caller.
        """
        from types import SimpleNamespace

        from agent.tools.pub_base.process_reap import kill_process_tree

        killed = 0
        for task in self.list_session(session_id):
            if task.status != "running" or task.pid is None:
                continue
            try:
                kill_process_tree(
                    SimpleNamespace(pid=task.pid),
                    pgid=task.pgid or task.pid,
                    grace=1.0,
                )
                killed += 1
            except (OSError, ProcessLookupError):
                pass  # already dead: the watcher already recorded that
            self.update(task.task_id, status="killed")
        return killed

    # ── the watcher ──────────────────────────────────────────────────────────

    def start_watcher(
        self, task_id: str, proc: asyncio.subprocess.Process, session_id: str
    ) -> None:
        """Watch ``proc`` to its exit, then notify (never blocks the caller)."""
        task = asyncio.create_task(self._watch(task_id, proc, session_id))
        self._watchers.add(task)
        task.add_done_callback(self._watchers.discard)

    async def _watch(self, task_id: str, proc: asyncio.subprocess.Process, session_id: str) -> None:
        """Await the child's exit and announce it. Fail-open at every step."""
        try:
            exit_code = await proc.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken handle is still an exit
            logger.warning("background task {}: wait failed: {}", task_id, exc)
            exit_code = -1
        task = self._tasks.get(task_id)
        if task is None:
            return
        self.update(
            task_id,
            status="completed" if exit_code == 0 else "failed",
            exit_code=exit_code,
        )
        logger.info(
            "background task {} (session {}) exited with code {}",
            task_id,
            session_id,
            exit_code,
        )
        if not TERMINAL_BG["notify_on_exit"]:
            return
        await self._push_exit_notification(task)
        await self._trigger_auto_turn(task)

    async def _push_exit_notification(self, task: BackgroundTask) -> None:
        """Best-effort WS frame (same shape/pattern as the TaskFlow progress push)."""
        try:
            from runtime.session.ws_push import broadcast_to_session

            payload = {
                "event": "terminal_bg_exit",
                "session_id": task.session_id,
                "content": {
                    "task_id": task.task_id,
                    "exit_code": task.exit_code,
                    "command": task.command,
                },
            }
            await broadcast_to_session(task.session_id, payload, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - a dead socket must not matter
            logger.debug("background task {}: WS push failed: {}", task.task_id, exc)

    async def _trigger_auto_turn(self, task: BackgroundTask) -> None:
        """Wake the agent with the exit report (same path the enforcer uses)."""
        try:
            from langchain_core.messages import HumanMessage

            from runtime import hooks

            trigger = hooks.resolve(hooks.MAYBE_TRIGGER_AUTO_TURN)
            if trigger is None:
                return  # evals / tests: no server owns the trigger
            session_key = f"agent:main:session:{task.session_id}"
            status = "exited" if task.exit_code == 0 else "FAILED"
            await trigger(
                session_key,
                HumanMessage(
                    content=(
                        f"[Background task {status}] {task.task_id} "
                        f"(command: {task.command}) exited with code {task.exit_code}. "
                        f'Read its output with terminal_bg_output(task_id="{task.task_id}").'
                    ),
                    metadata={"origin": "background_task", "internal": True},
                ),
            )
        except Exception as exc:  # noqa: BLE001 - the notice is best-effort
            logger.debug("background task {}: auto-turn trigger failed: {}", task.task_id, exc)


#: The process-level singleton (tasks are children of THIS process).
background_registry = BackgroundTaskRegistry()


def output_read_limit() -> int:
    """Tail bytes ``terminal_bg_output`` returns (from the registry config)."""
    return int(TERMINAL_BG["output_max_read_bytes"])


def read_output_tail(path: Path, max_bytes: int | None = None) -> str:
    """The LAST ``max_bytes`` of a background task's output file.

    A bounded tail read: ``stat`` the size, seek to ``size - limit`` and read —
    a multi-GB log costs the same as a small one. The label names the total so
    the model knows the head was skipped.
    """
    limit = int(max_bytes if max_bytes is not None else TERMINAL_BG["output_max_read_bytes"])
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size == 0:
        return ""
    offset = max(0, size - limit)
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if offset > 0:
        return f"...[showing the last {len(data)} of {size} bytes]\n{text}"
    return text


def open_background_output(session_id: str, task_id: str) -> Path | None:
    """Create (and return) the retained output file for one background task.

    Retained means RETAINED: the file lives under the session's ``evicted/``
    directory — the same place every other oversized payload is kept, so session
    deletion removes it and nothing sweeps it while the task is alive. ``None``
    for an unsafe session id (nothing is written outside ``SESSIONS_DIR``).
    """
    directory = get_eviction_dir(session_id)
    if directory is None:
        return None
    path = directory / f"terminal-bg-{task_id}.log"
    path.touch()
    return path


def discard_background_output(task: BackgroundTask) -> None:
    """Remove a task's output file (refused spawn, session teardown)."""
    if task.output_path is None:
        return
    try:
        task.output_path.unlink(missing_ok=True)
    except OSError:
        logger.debug("background task {}: output file removal failed", task.task_id)
