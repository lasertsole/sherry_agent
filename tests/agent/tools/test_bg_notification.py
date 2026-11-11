"""ZCode-aligned ``<task-notification>`` content for background-task auto-turns."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.terminal.bg_registry import (
    BackgroundTask,
    _build_task_notification,
    _normalize_status,
)

pytestmark = [pytest.mark.unit]


def _task(
    *,
    status: str,
    exit_code: int | None = None,
    command: str = "uv run python -m server",
    output_path: Path | None = Path("/tmp/session/terminal-bg-bg-1.log"),
) -> BackgroundTask:
    return BackgroundTask(
        task_id="bg-1",
        session_id="s1",
        pid=42,
        command=command,
        output_path=output_path,
        started_at=0.0,
        status=status,
        exit_code=exit_code,
    )


def _tags(content: str) -> dict[str, str]:
    return {
        key: content.split(f"<{key}>", 1)[1].split(f"</{key}>", 1)[0]
        for key in ("task-id", "output-file", "status", "summary")
        if f"<{key}>" in content
    }


def test_killed_task_notification_matches_zcode():
    content = _build_task_notification(_task(status="killed", exit_code=-15))

    assert content.startswith("<task-notification>\n<task-id>bg-1</task-id>\n")
    assert content.endswith("</task-notification>")
    assert _tags(content) == {
        "task-id": "bg-1",
        "output-file": "/tmp/session/terminal-bg-bg-1.log",
        "status": "killed",
        "summary": 'Background command "uv run python -m server" was stopped',
    }
    # No self-authored restart instruction: asking is emergent model behaviour.
    assert "restart" not in content.lower()
    assert "Read its output" not in content


@pytest.mark.parametrize("status", ["cancelled", "timed_out", "stopped"])
def test_stop_shaped_statuses_normalize_to_killed(status):
    assert _normalize_status(status) == "killed"
    assert "<status>killed</status>" in _build_task_notification(
        _task(status=status, exit_code=None)
    )


def test_completed_task_notification_carries_the_exit_code():
    content = _build_task_notification(_task(status="completed", exit_code=0))

    assert _tags(content)["status"] == "completed"
    assert _tags(content)["summary"] == (
        'Background command "uv run python -m server" completed (exit code 0)'
    )


def test_failed_task_notification_carries_the_exit_code():
    content = _build_task_notification(_task(status="failed", exit_code=2))

    assert _tags(content)["status"] == "failed"
    assert _tags(content)["summary"] == (
        'Background command "uv run python -m server" failed with exit code 2'
    )


def test_unknown_status_is_reported_as_failed():
    assert _normalize_status("running") == "failed"
    assert "<status>failed</status>" in _build_task_notification(_task(status="running"))


def test_a_task_without_an_output_file_omits_the_element():
    content = _build_task_notification(_task(status="killed", output_path=None))

    assert "<output-file>" not in content
    assert "<task-id>bg-1</task-id>" in content


def test_a_missing_command_uses_the_generic_description():
    content = _build_task_notification(_task(status="killed", command=""))

    assert 'Background command "Bash background command" was stopped' in content


def test_exit_code_is_omitted_when_unknown():
    content = _build_task_notification(_task(status="completed", exit_code=None))

    assert _tags(content)["summary"] == 'Background command "uv run python -m server" completed'
