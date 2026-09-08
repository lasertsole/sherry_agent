"""TDD tests for audit 3.1.4 — shared channel dependency installer
(channels/deps.py) wired into channels/registry.py and plugins/channels/qq.

Pins the install flow both callers used to duplicate: uv preferred, pip
fallback, 120 s timeout, import-cache invalidation on success, and each
caller's distinct failure policy / log wording.
"""

import subprocess
import sys

import pytest

import channels.deps as deps
from channels.deps import install_requirements

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _FakeResult:
    def __init__(self, returncode=0, stderr="", stdout=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


@pytest.fixture(autouse=True)
def _no_uv(monkeypatch):
    monkeypatch.setattr(deps.shutil, "which", lambda name: None)


class TestInstallRequirements:
    def test_uses_uv_when_available(self, monkeypatch, tmp_path):
        recorded = {}

        def _fake_run(cmd, **kwargs):
            recorded["cmd"] = cmd
            recorded["kwargs"] = kwargs
            return _FakeResult(0)

        monkeypatch.setattr(
            deps.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
        )
        monkeypatch.setattr(deps.subprocess, "run", _fake_run)
        invalidated: list[bool] = []
        monkeypatch.setattr(deps.importlib, "invalidate_caches", lambda: invalidated.append(True))

        ok, timed_out, stderr = install_requirements(tmp_path / "requirements.txt")

        assert (ok, timed_out, stderr) == (True, False, "")
        assert recorded["cmd"] == [
            "uv",
            "pip",
            "install",
            "-q",
            "-r",
            str(tmp_path / "requirements.txt"),
        ]
        assert invalidated == [True]

    def test_falls_back_to_python_m_pip(self, monkeypatch, tmp_path):
        recorded = {}

        def _fake_run(cmd, **kwargs):
            recorded["cmd"] = cmd
            return _FakeResult(0)

        monkeypatch.setattr(deps.subprocess, "run", _fake_run)

        ok, _, _ = install_requirements(tmp_path / "requirements.txt")

        assert ok is True
        assert recorded["cmd"] == [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-q",
            "-r",
            str(tmp_path / "requirements.txt"),
        ]

    def test_timeout_returns_false_true(self, monkeypatch, tmp_path):
        def _fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)

        monkeypatch.setattr(deps.subprocess, "run", _fake_run)

        ok, timed_out, stderr = install_requirements(tmp_path / "requirements.txt")

        assert (ok, timed_out, stderr) == (False, True, "")

    def test_failure_returns_stderr_summary(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            deps.subprocess, "run", lambda *a, **k: _FakeResult(1, stderr="no such package\n")
        )
        invalidated: list[bool] = []
        monkeypatch.setattr(deps.importlib, "invalidate_caches", lambda: invalidated.append(True))

        ok, timed_out, stderr = install_requirements(tmp_path / "requirements.txt")

        assert (ok, timed_out) == (False, False)
        assert stderr == "no such package"
        assert invalidated == []  # caches only invalidated on success

    def test_stderr_falls_back_to_stdout(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            deps.subprocess, "run", lambda *a, **k: _FakeResult(1, stdout="out only")
        )

        _, _, stderr = install_requirements(tmp_path / "requirements.txt")

        assert stderr == "out only"


class TestRegistryWiring:
    def test_ensure_deps_true_without_requirements(self, tmp_path):
        from channels import registry

        assert registry._ensure_deps(tmp_path, "mychan") is True

    def test_ensure_deps_wires_timeout_and_error_paths(self, tmp_path, monkeypatch):
        from channels import registry
        from loguru import logger

        (tmp_path / "requirements.txt").write_text("botpy\n", encoding="utf-8")
        logs: list[str] = []
        sink_id = logger.add(lambda m: logs.append(str(m)), level="DEBUG")

        try:
            monkeypatch.setattr(
                registry, "install_requirements", lambda req, **k: (False, True, "")
            )
            assert registry._ensure_deps(tmp_path, "c1") is False
            assert any("Timed out installing dependencies for channel 'c1'" in m for m in logs)

            monkeypatch.setattr(
                registry, "install_requirements", lambda req, **k: (False, False, "bad")
            )
            assert registry._ensure_deps(tmp_path, "c1") is False
            assert any("Failed to install dependencies for channel 'c1'" in m for m in logs)

            monkeypatch.setattr(
                registry, "install_requirements", lambda req, **k: (True, False, "")
            )
            assert registry._ensure_deps(tmp_path, "c1") is True
            assert any("Dependencies for channel 'c1' ready" in m for m in logs)
        finally:
            logger.remove(sink_id)


class TestQQWiring:
    def test_qq_install_records_failures_and_resets_cooldown(self, monkeypatch, tmp_path):
        import importlib.util
        import pathlib

        core_path = pathlib.Path("plugins/channels/qq/core.py")
        spec = importlib.util.spec_from_file_location("channels.plugin.qq_test", str(core_path))
        qq = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(qq)

        recorded: list[str] = []
        monkeypatch.setattr(
            qq,
            "install_requirements",
            lambda req, **k: recorded.append("call") or (True, False, ""),
        )
        monkeypatch.setattr(qq, "_reset_cooldown", lambda: recorded.append("reset"))

        ok = qq._install_deps()

        assert ok is True
        assert recorded == ["call", "reset"]

        monkeypatch.setattr(qq, "install_requirements", lambda req, **k: (False, True, ""))
        monkeypatch.setattr(qq, "_record_install_failure", lambda: recorded.append("fail"))
        assert qq._install_deps() is False
        assert recorded[-1] == "fail"
