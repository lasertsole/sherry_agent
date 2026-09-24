"""clawhub subprocess hygiene.

``run_clawhub_command`` executes remote npm code, so the invocation must be
pinned to an exact package version (never ``@latest``) and the child process
must run on a scrubbed environment — the parent's API keys must not leak into
npm/node.
"""

import subprocess
from pathlib import Path

import pytest

from skills.builtin.core.clawhub.scripts import clawhub_runner

pytestmark = [pytest.mark.unit]


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(clawhub_runner.subprocess, "run", _fake_run)
    return captured


def test_package_spec_is_pinned_not_latest():
    assert clawhub_runner._CLAWHUB_SPEC.startswith("clawhub@")
    assert clawhub_runner._CLAWHUB_SPEC != "clawhub@latest"
    version = clawhub_runner._CLAWHUB_SPEC.split("@", 1)[1]
    assert version and version[0].isdigit()


def test_run_uses_pinned_spec_and_scrubbed_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captured = _capture_run(monkeypatch)
    # Plant a real secret so the scrub has something to strip (scrub_env reads
    # os.environ at call time, so setenv is the effective injection point).
    monkeypatch.setenv("MAIN_LLM_API_KEY", "planted-secret")

    payload = clawhub_runner.run_clawhub_command(["search", "demo"])

    assert payload["success"] is True
    assert captured["cmd"][:3] == ["npx", "--yes", clawhub_runner._CLAWHUB_SPEC]
    env = captured["kwargs"]["env"]
    assert env is not None
    assert "MAIN_LLM_API_KEY" not in env


def test_pinned_spec_still_detected_by_hitl_gate():
    """The HITL remote-npm gate must keep matching the pinned invocation."""
    from agent.middlewares.humanInTheLoop import CLAWHUB_REMOTE_NPM_TAG, detect_clawhub_command

    cmd = f"npx --yes {clawhub_runner._CLAWHUB_SPEC} install demo --workdir x"
    assert detect_clawhub_command(cmd) == CLAWHUB_REMOTE_NPM_TAG


def test_scrubbed_env_still_allows_node_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captured = _capture_run(monkeypatch)

    clawhub_runner.run_clawhub_command(["search", "demo"])

    env = captured["kwargs"]["env"]
    # npx/node need PATH; the scrub keeps the process usable, not empty.
    assert env.get("PATH")
