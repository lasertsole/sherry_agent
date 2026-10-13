"""Unit tests for the built-in skill security scan (skills.skills_snapshot).

The scanner is resolved through ``runtime.hooks`` (registered by the server
assembly). These tests pin the migration semantics:

* a missing hook skips the scan without raising (fail-open — exactly the
  pre-hooks ImportError path);
* a registered hook is called once per existing built-in target;
* a raising scanner is swallowed per target (fail-open);
* a missing target file is skipped silently.
"""

from types import SimpleNamespace

import pytest

from runtime import hooks
from skills import skills_snapshot

pytestmark = [pytest.mark.unit]

_TARGET_REL = "builtin/core/clawhub/SKILL.md"


def _scan_result(*, unavailable: bool = False, do_not_install: bool = False, caution: bool = False):
    return SimpleNamespace(
        is_unavailable=unavailable,
        is_do_not_install=do_not_install,
        is_caution=caution,
        risk_score=50,
    )


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point the builtin scan targets at tmp dirs and clear the scan hooks."""
    hooks.unregister(hooks.SCAN_SKILL)
    monkeypatch.setattr(skills_snapshot, "SKILLS_DIR", tmp_path)
    yield
    hooks.unregister(hooks.SCAN_SKILL)


def _make_target(tmp_path) -> None:
    skill_md = tmp_path / _TARGET_REL
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("---\nname: clawhub\n---\nbody\n", encoding="utf-8")


class TestMissingHook:
    def test_skips_scan_without_raising(self, tmp_path):
        _make_target(tmp_path)

        skills_snapshot._scan_builtin_skills()


class TestRegisteredHook:
    def test_calls_scanner_once_per_existing_target(self, tmp_path):
        _make_target(tmp_path)
        calls: list[str] = []

        def fake_scan(path):
            calls.append(str(path))
            return _scan_result()

        hooks.register(hooks.SCAN_SKILL, fake_scan)

        skills_snapshot._scan_builtin_skills()

        assert calls == [str(tmp_path / _TARGET_REL)]

    def test_scan_exception_is_swallowed(self, tmp_path):
        _make_target(tmp_path)
        calls: list[str] = []

        def fake_scan(path):
            calls.append(str(path))
            raise RuntimeError("scanner exploded")

        hooks.register(hooks.SCAN_SKILL, fake_scan)

        skills_snapshot._scan_builtin_skills()

        assert len(calls) == 1

    def test_missing_target_is_skipped(self, tmp_path):
        calls: list[str] = []
        hooks.register(hooks.SCAN_SKILL, lambda path: calls.append(str(path)))

        skills_snapshot._scan_builtin_skills()

        assert calls == []
