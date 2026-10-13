"""Windows Git Bash detection (``agent/tools/pub_base/shell_resolver.py``).

The terminal tool promises a POSIX shell: ``&`` backgrounds, ``&&`` short-
circuits, ``$()`` substitutes. ``cmd.exe`` — Windows' default — changes every
one of those, so a Git-for-Windows install is preferred when it exists.
"""

from __future__ import annotations

import pytest

from agent.tools.pub_base import shell_resolver as sr

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _fresh_cache():
    sr.reset_windows_bash_cache()
    yield
    sr.reset_windows_bash_cache()


class TestResolution:
    def test_non_windows_is_none(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(sr.platform, "system", lambda: "Linux")

        assert sr.resolve_windows_bash() is None

    def test_a_standard_install_location_wins(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(sr.platform, "system", lambda: "Windows")
        target = sr._GIT_BASH_CANDIDATES[0]
        monkeypatch.setattr(sr, "_is_executable", lambda path: path == target)

        assert sr.resolve_windows_bash() == target

    def test_git_exe_on_path_infers_bash(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        monkeypatch.setattr(sr.platform, "system", lambda: "Windows")
        git_dir = tmp_path / "Git" / "cmd"
        git_dir.mkdir(parents=True)
        git_exe = git_dir / "git.exe"
        git_exe.write_text("", encoding="utf-8")
        bash = tmp_path / "Git" / "bin" / "bash.exe"
        bash.parent.mkdir(parents=True)
        bash.write_text("", encoding="utf-8")
        monkeypatch.setattr(sr.shutil, "which", lambda name: str(git_exe))
        monkeypatch.setattr(sr, "_is_executable", lambda path: str(path) == str(bash))

        assert sr.resolve_windows_bash() == str(bash)

    def test_no_git_bash_keeps_the_platform_shell(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(sr.platform, "system", lambda: "Windows")
        monkeypatch.setattr(sr, "_is_executable", lambda path: False)
        monkeypatch.setattr(sr.shutil, "which", lambda name: None)

        assert sr.resolve_windows_bash() is None

    def test_the_probe_is_cached(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(sr.platform, "system", lambda: "Windows")
        calls: list[str] = []

        def _probe(path: str) -> bool:
            calls.append(path)
            return False

        monkeypatch.setattr(sr, "_is_executable", _probe)
        monkeypatch.setattr(sr.shutil, "which", lambda name: None)

        sr.resolve_windows_bash()
        first = len(calls)
        sr.resolve_windows_bash()

        assert len(calls) == first, "a second call must not probe again"
