"""Hermetic tests for the container-backed benchmark bridge.

These cover the parts that decide a verdict before Docker is involved: pytest
summary parsing, DeepSWE reward parsing, and the ``terminal`` tool swap. No
Docker, no model, no network.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from evals import external_bench

pytestmark = [pytest.mark.unit]

_PASS_OUTPUT = """\
tests/test_outputs.py::test_hello PASSED
============================ short test summary info =============================
PASSED tests/test_outputs.py::test_hello
1 passed in 0.01s
"""

_FAIL_OUTPUT = """\
============================ short test summary info =============================
FAILED tests/test_x.py::test_a - assert 1 == 2
PASSED tests/test_x.py::test_b
XFAIL tests/test_x.py::test_c
"""


def test_parse_pytest_output_passing():
    parsed = external_bench.parse_pytest_output(_PASS_OUTPUT)

    assert parsed == {"test_hello": "PASSED"}
    assert external_bench.pytest_resolved(parsed)


def test_parse_pytest_output_failure_and_nonpassing_states():
    parsed = external_bench.parse_pytest_output(_FAIL_OUTPUT)

    assert parsed == {
        "test_a": "FAILED",
        "test_b": "PASSED",
        "test_c": "XFAIL",
    }
    assert not external_bench.pytest_resolved(parsed)


def test_parse_pytest_output_without_summary_is_none():
    """A runner that never reached pytest must not be vacuously resolved."""
    assert external_bench.parse_pytest_output("bash: pytest: not found") is None
    assert not external_bench.pytest_resolved(None)
    assert not external_bench.pytest_resolved({})


def test_parse_pytest_output_strips_ansi_codes():
    parsed = external_bench.parse_pytest_output(
        "= short test summary info =\n\x1b[32mPASSED\x1b[0m tests/test_a.py::test_ok\n"
    )

    assert parsed == {"test_ok": "PASSED"}


def test_parse_pytest_output_reads_all_summaries():
    """run-tests.sh may invoke pytest twice; both summaries judge the task."""
    output = (
        "= short test summary info =\n"
        "FAILED tests/test_a.py::test_a\n"
        "= short test summary info =\n"
        "PASSED tests/test_b.py::test_b\n"
    )

    parsed = external_bench.parse_pytest_output(output)

    assert parsed == {"test_a": "FAILED", "test_b": "PASSED"}
    assert not external_bench.pytest_resolved(parsed)


def test_read_reward_json_and_text(tmp_path: Path):
    good = tmp_path / "good"
    good.mkdir()
    (good / "reward.json").write_text(
        json.dumps({"reward": 1, "f2p": 3, "p2p": 4}), encoding="utf-8"
    )
    payload = external_bench.read_reward(good)
    assert payload is not None and external_bench.reward_resolved(payload)

    zero = tmp_path / "zero"
    zero.mkdir()
    (zero / "reward.json").write_text(json.dumps({"reward": 0}), encoding="utf-8")
    assert not external_bench.reward_resolved(external_bench.read_reward(zero))

    crash = tmp_path / "crash"
    crash.mkdir()
    (crash / "reward.txt").write_text("-1\n", encoding="utf-8")
    payload = external_bench.read_reward(crash)
    assert payload is not None and not external_bench.reward_resolved(payload)

    assert external_bench.read_reward(tmp_path / "missing") is None


def test_docker_available_reports_missing_cli(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(external_bench.shutil, "which", lambda _name: None)

    ok, detail = external_bench.docker_available()

    assert not ok
    assert "not on PATH" in detail


def test_bridge_tool_replaces_terminal(monkeypatch: pytest.MonkeyPatch):
    """The model-facing tool keeps its name/schema; execution is redirected."""
    import agent.core as core
    from agent.tools.terminal import SafeShellTool

    class FakeBridge:
        def exec(self, command: str, *, timeout: float = 300.0) -> str:
            return f"exec:{command}:{timeout:.0f}"

        async def aexec(self, command: str, *, timeout: float = 300.0) -> str:
            return f"aexec:{command}:{timeout:.0f}"

    original = core._tools
    original_tool = SafeShellTool()
    core._tools = [original_tool]
    try:
        external_bench.install_terminal_bridge(FakeBridge())
        tool = core.get_agent_tools()[0]

        assert tool is not original_tool
        assert tool.name == "terminal"
        assert tool.args_schema.__name__ == "SafeShellInput"
        assert tool.metadata == {"idempotent": False, "side_effect_scope": "system"}
        assert tool.invoke({"commands": ["echo hi", "pwd"]}) == "exec:echo hi && pwd:30"
    finally:
        core._tools = original


def test_localize_instruction_maps_container_paths():
    host = Path("/tmp/eval/work")

    text = external_bench.localize_instruction(
        "Write /app/hello.txt and ls /app; /application stays.", host, "/app"
    )

    assert str(host) in text
    assert "/app/hello.txt" not in text
    assert "/application stays" in text
    assert "/foo/app/x" == external_bench.localize_instruction("/foo/app/x", host, "/app")
    assert external_bench.localize_instruction("/app", host, "/app") == str(host)


def test_image_tag_and_container_name_are_docker_safe():
    tag = external_bench.image_tag("terminal-bench", "Hello World/Task", "abc123")

    assert tag.startswith("sherry-eval-terminal-bench-")
    assert " " not in tag and "/" not in tag

    name = external_bench.container_name("deepswe", "a/b c")
    assert " " not in name and "/" not in name


def test_create_container_command_shape(monkeypatch: pytest.MonkeyPatch):
    recorded: list[list[str]] = []

    def fake_docker(args: list[str], *, timeout: float = 120.0) -> external_bench.CommandResult:
        recorded.append(args)
        return external_bench.CommandResult(0, "container-id\n")

    monkeypatch.setattr(external_bench, "_docker", fake_docker)

    cid = external_bench.create_container(
        "image:tag",
        "sherry-eval-test",
        workdir="/w",
        mounts=(("/host/tree", "/w"),),
        network="none",
        env={"TEST_DIR": "/tests"},
    )

    assert cid == "container-id"
    args = recorded[0]
    assert args[:5] == ["run", "-d", "--name", "sherry-eval-test", "--label"]
    assert "/host/tree:/w" in args
    assert args[-3:] == ["image:tag", "-c", "sleep 2147483647"]
    assert "-e" in args and "TEST_DIR=/tests" in args
    assert "--network" in args and "none" in args


def _fake_docker(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "docker"
    script.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" > "$DOCKER_ARGS_FILE"\n'
        "echo fake-stdout\n"
        "echo fake-stderr >&2\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return bin_dir


@pytest.mark.skipif(os.name == "nt", reason="the fake docker is a POSIX shell script")
def test_bridge_exec_sends_the_persistent_shell_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bin_dir = _fake_docker(tmp_path)
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("DOCKER_ARGS_FILE", str(args_file))
    bridge = external_bench.DockerBridge("cid", host_workdir=Path("/host/tree"))

    output = bridge.exec("echo hi", timeout=5)

    assert "fake-stdout" in output and "fake-stderr" in output
    args = args_file.read_text(encoding="utf-8")
    assert "exec\n" in args
    assert "-w\n/host/tree\ncid\n/bin/bash\n-lc\n" in args
    assert "echo hi" in args
    assert "sleep 5" in args
    assert "export -p" in args


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="the fake docker is a POSIX shell script")
async def test_bridge_aexec_uses_the_async_process_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bin_dir = _fake_docker(tmp_path)
    args_file = tmp_path / "args.txt"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("DOCKER_ARGS_FILE", str(args_file))
    bridge = external_bench.DockerBridge("cid", host_workdir=Path("/host/tree"))

    output = await bridge.aexec("echo hi", timeout=5)

    assert "fake-stdout" in output and "fake-stderr" in output
    assert "echo hi" in args_file.read_text(encoding="utf-8")
