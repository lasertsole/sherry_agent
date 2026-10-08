"""The toolbox's user terminal: one command in, its output out.

The service is a thin, bounded wrapper over a shell spawn, so these tests pin the
contract the panel depends on: the command runs in the SESSION'S project
directory (the selected 工作目录, not the checkout), a non-zero exit code is a
normal answer, a wedged command is killed and reported, and the environment is
scrubbed before the child sees it.
"""

from __future__ import annotations

import asyncio

import pytest

from server.service import user_terminal_service as service
from server.trigger.http import terminal as http

pytestmark = [pytest.mark.unit]


class _FakeRequest:
    """Minimal Robyn-request double: ``query_params`` for GET, ``json()`` for POST."""

    def __init__(self, query_params: dict | None = None, body: dict | None = None):
        self.query_params = query_params or {}
        self._body = body or {}

    def json(self) -> dict:
        """Robyn's JSON body reader (``read_body`` calls exactly this)."""
        return self._body


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project directory the session is bound to."""
    monkeypatch.setattr(service, "current_project_dir", lambda sid: tmp_path)
    return tmp_path


def test_a_command_runs_in_the_session_project_directory(project):
    result = asyncio.run(service.run_user_command("s1", "pwd && echo hello"))

    assert result["cwd"] == str(project)
    assert result["exit_code"] == 0
    assert str(project) in str(result["output"])
    assert "hello" in str(result["output"])
    assert int(result["duration_ms"]) >= 0


def test_a_failing_command_reports_its_exit_code(project):
    result = asyncio.run(service.run_user_command("s1", "echo boom >&2; exit 3"))

    assert result["exit_code"] == 3
    # stderr is folded into the same stream (a console shows one log).
    assert "boom" in str(result["output"])


def test_output_is_capped_and_flagged(project, monkeypatch):
    monkeypatch.setattr(service, "_MAX_OUTPUT_CHARS", 200)

    result = asyncio.run(service.run_user_command("s1", "yes x | head -c 5000"))

    assert result["truncated"] is True
    assert len(str(result["output"])) == 200


def test_a_wedged_command_is_killed_and_named(project, monkeypatch):
    monkeypatch.setattr(service, "_TIMEOUT_S", 0.2)

    result = asyncio.run(service.run_user_command("s1", "sleep 30"))

    assert result["exit_code"] == -1
    assert "timed out" in str(result["output"])


def test_the_environment_is_scrubbed(project, monkeypatch):
    monkeypatch.setenv("SHERRY_TEST_SECRET", "sk-do-not-leak")

    result = asyncio.run(service.run_user_command("s1", "echo [$SHERRY_TEST_SECRET]"))

    # scrub_env drops key-shaped variables, so the child cannot read them.
    assert "sk-do-not-leak" not in str(result["output"])


def test_an_empty_command_is_refused(project):
    with pytest.raises(ValueError, match="command is required"):
        asyncio.run(service.run_user_command("s1", "   "))
    with pytest.raises(ValueError, match="session_id is required"):
        asyncio.run(service.run_user_command("", "ls"))


def test_an_oversized_command_is_refused(project):
    with pytest.raises(ValueError, match="too long"):
        asyncio.run(service.run_user_command("s1", "x" * (service._MAX_COMMAND_CHARS + 1)))


def test_terminal_info_names_the_working_directory(project):
    info = service.read_terminal_info("s1")

    assert info["cwd"] == str(project)
    assert info["shell"] == "/bin/sh"
    with pytest.raises(ValueError):
        service.read_terminal_info("")


def test_the_routes_serve_info_and_a_run(monkeypatch):
    monkeypatch.setattr(
        http, "read_terminal_info", lambda sid: {"cwd": "/tmp/p", "shell": "/bin/sh"}
    )

    async def fake_run(session_id: str, command: str) -> dict:
        return {
            "cwd": "/tmp/p",
            "command": command,
            "exit_code": 0,
            "output": command,
            "truncated": False,
        }

    monkeypatch.setattr(http, "run_user_command", fake_run)

    info = asyncio.run(http.terminal_info_handler(_FakeRequest({"session_id": "s1"})))
    assert info.status_code == 200
    import json

    assert json.loads(info.description)["cwd"] == "/tmp/p"

    ran = asyncio.run(
        http.terminal_run_handler(_FakeRequest(body={"session_id": "s1", "command": "ls"}))
    )
    assert ran.status_code == 200
    assert json.loads(ran.description)["command"] == "ls"


def test_the_run_route_refuses_a_missing_session_or_command(monkeypatch):
    monkeypatch.setattr(http, "run_user_command", service.run_user_command)

    missing = asyncio.run(http.terminal_run_handler(_FakeRequest(body={"command": "ls"})))
    empty = asyncio.run(http.terminal_run_handler(_FakeRequest(body={"session_id": "s1"})))

    assert missing.status_code == 400
    assert empty.status_code == 400
