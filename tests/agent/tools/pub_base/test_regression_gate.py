"""Finish-time re-verification: broken/unmark events and the command policy."""

from __future__ import annotations

from pathlib import Path

import pytest

import agent.tools.pub_base.regression_gate as regression_gate
from agent.tools.pub_base.evidence_ledger import EvidenceLedger
from agent.tools.taskflow.evidence_collector import collect_evidence_summary
from config.features import EVIDENCE_REGRESSION_GATE

pytestmark = [pytest.mark.unit]

COMMAND = "python -m pytest tests/test_feature.py -q"


@pytest.fixture(autouse=True)
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EvidenceLedger:
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(tmp_path / "evidence.db"))
    monkeypatch.setitem(EVIDENCE_REGRESSION_GATE, "enabled", True)
    monkeypatch.setitem(EVIDENCE_REGRESSION_GATE, "max_reverify_commands", 10)
    monkeypatch.setitem(EVIDENCE_REGRESSION_GATE, "reverify_timeout_s", 5)
    return EvidenceLedger


def _evidence(session: str, command: str = COMMAND) -> None:
    EvidenceLedger.for_session(session).append(kind="test", command=command, status="passed")


async def _fake_run(exit_code: int | None, output: str = ""):
    async def run(_argv, _cwd, _timeout):
        return exit_code, output

    return run


@pytest.mark.asyncio
async def test_a_failed_replay_appends_broken_and_renders_it(monkeypatch: pytest.MonkeyPatch):
    _evidence("session-a")
    monkeypatch.setattr(regression_gate, "_run", await _fake_run(1, "1 failed"))

    results = await regression_gate.reverify_flow_evidence(["session-a"])

    assert [result.outcome for result in results] == ["failed"]
    summary = collect_evidence_summary(session_key="session-a") or ""
    assert "[broken]" in summary and "[stale]" not in summary
    broken = EvidenceLedger.read_for_session("session-a")[-1]
    assert broken["event"] == "broken"
    assert broken["command"] == COMMAND
    assert broken["scope"] == "regression_gate"


@pytest.mark.asyncio
async def test_a_passing_replay_unmarks_a_false_positive(monkeypatch: pytest.MonkeyPatch):
    _evidence("session-a")
    EvidenceLedger.for_session("session-a").mark_stale_for_path("tests/test_feature.py")
    assert "[stale]" in (collect_evidence_summary(session_key="session-a") or "")
    monkeypatch.setattr(regression_gate, "_run", await _fake_run(0, "1 passed"))

    results = await regression_gate.reverify_flow_evidence(["session-a"])

    assert [result.outcome for result in results] == ["passed"]
    summary = collect_evidence_summary(session_key="session-a") or ""
    assert "[stale]" not in summary and "[broken]" not in summary
    assert EvidenceLedger.read_for_session("session-a")[-1]["event"] == "unmark"


@pytest.mark.asyncio
async def test_duplicate_evidence_rows_append_one_event_per_session(
    monkeypatch: pytest.MonkeyPatch,
):
    _evidence("session-a")
    _evidence("session-a")
    monkeypatch.setattr(regression_gate, "_run", await _fake_run(1, "1 failed"))

    await regression_gate.reverify_flow_evidence(["session-a"])

    broken_rows = [
        row for row in EvidenceLedger.read_for_session("session-a") if row.get("event") == "broken"
    ]
    assert len(broken_rows) == 1


@pytest.mark.asyncio
async def test_a_passing_replay_without_invalidation_appends_nothing(
    monkeypatch: pytest.MonkeyPatch,
):
    _evidence("session-a")
    monkeypatch.setattr(regression_gate, "_run", await _fake_run(0))

    await regression_gate.reverify_flow_evidence(["session-a"])

    assert len(EvidenceLedger.read_for_session("session-a")) == 1


@pytest.mark.asyncio
async def test_a_timeout_is_skipped_and_never_marks_broken(monkeypatch: pytest.MonkeyPatch):
    _evidence("session-a")
    monkeypatch.setattr(regression_gate, "_run", await _fake_run(None, "timed out"))

    results = await regression_gate.reverify_flow_evidence(["session-a"])

    assert [result.outcome for result in results] == ["skipped"]
    assert len(EvidenceLedger.read_for_session("session-a")) == 1


@pytest.mark.asyncio
async def test_an_unsafe_command_is_skipped_without_replay(monkeypatch: pytest.MonkeyPatch):
    _evidence("session-a", "pytest; rm -rf /")

    async def _should_not_run(*_args, **_kwargs):
        raise AssertionError("unsafe command was replayed")

    monkeypatch.setattr(regression_gate, "_run", _should_not_run)

    results = await regression_gate.reverify_flow_evidence(["session-a"])

    assert [result.outcome for result in results] == ["skipped"]
    assert len(EvidenceLedger.read_for_session("session-a")) == 1


@pytest.mark.asyncio
async def test_the_command_cap_limits_the_batch(monkeypatch: pytest.MonkeyPatch):
    for index in range(5):
        _evidence(f"session-{index}", f"ruff check file_{index}.py")
    monkeypatch.setitem(EVIDENCE_REGRESSION_GATE, "max_reverify_commands", 2)
    seen: list[str] = []

    async def run(argv, _cwd, _timeout):
        seen.append(argv[-1])
        return 0, ""

    monkeypatch.setattr(regression_gate, "_run", run)

    results = await regression_gate.reverify_flow_evidence([f"session-{i}" for i in range(5)])

    assert len(results) == 2
    assert seen == ["file_0.py", "file_1.py"]


@pytest.mark.asyncio
async def test_disabling_the_gate_does_nothing(monkeypatch: pytest.MonkeyPatch):
    _evidence("session-a")
    monkeypatch.setitem(EVIDENCE_REGRESSION_GATE, "enabled", False)

    async def _should_not_run(*_args, **_kwargs):
        raise AssertionError("disabled gate replayed a command")

    monkeypatch.setattr(regression_gate, "_run", _should_not_run)

    assert await regression_gate.reverify_flow_evidence(["session-a"]) == []


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        ("python -m pytest -q", True),
        ("uv run pytest -q", True),
        ("npm test", True),
        ("npx vitest run", True),
        ("pnpm exec vitest run", True),
        ("ruff check .", True),
        ("npx definitely-not-a-verifier", False),
        ("pnpm exec definitely-not-a-verifier", False),
        ("pytest; rm -rf /", False),
        ("pytest && echo done", False),
        ("bash -c pytest", False),
        ("python -c 'import pytest'", False),
        ("rm -rf /", False),
    ],
)
def test_the_replay_command_policy(command: str, allowed: bool):
    assert (regression_gate._safe_argv(command) is not None) is allowed


class TestTheAllowlistIsConfigurable:
    """The verifier allowlist = code defaults + the user's sherry.jsonc entries.

    Same shape as the YOLO deny list: the consumer owns the defaults and the
    user's list is merged per call, so a new language toolchain needs a config
    line rather than a code change.
    """

    def test_defaults_apply_without_a_config_file(self, monkeypatch):
        import config.sherry_settings as sherry_settings

        # A missing file is the always-boot path: the loader returns [].
        monkeypatch.setattr(sherry_settings, "SHERRY_CONFIG_PATH", Path("/nonexistent.jsonc"))

        assert regression_gate._get_allowed_executables() == regression_gate._ALLOWED_EXECUTABLES

    def test_a_user_entry_is_merged_and_lowercased(self, monkeypatch, tmp_path):
        import config.sherry_settings as sherry_settings

        config = tmp_path / "sherry.jsonc"
        config.write_text(
            '{\n  "regression_gate_allowed_executables": ["Gradle", "  maven  "],\n}\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(sherry_settings, "SHERRY_CONFIG_PATH", config)

        merged = regression_gate._get_allowed_executables()

        assert {"gradle", "maven"} <= merged
        assert regression_gate._ALLOWED_EXECUTABLES <= merged, "defaults always survive"

    def test_the_configured_toolchain_replays(self, monkeypatch):
        import config.sherry_settings as sherry_settings

        assert regression_gate._safe_argv("gradle test") is None, "not allowed by default"

        def _user_entries(_key: str) -> list[str]:
            return ["gradle"]

        monkeypatch.setattr(sherry_settings, "load_sherry_list_setting", _user_entries)

        assert regression_gate._safe_argv("gradle test") == ["gradle", "test"]

    def test_a_tool_that_is_still_unlisted_is_refused(self, monkeypatch):
        import config.sherry_settings as sherry_settings

        monkeypatch.setattr(sherry_settings, "load_sherry_list_setting", lambda _key: ["gradle"])

        assert regression_gate._safe_argv("ruby spec.rb") is None
        assert regression_gate._safe_argv("php artisan test") is None

    def test_shell_metacharacters_stay_refused_even_when_allowed(self, monkeypatch):
        import config.sherry_settings as sherry_settings

        monkeypatch.setattr(sherry_settings, "load_sherry_list_setting", lambda _key: ["gradle"])

        assert regression_gate._safe_argv("gradle test && rm -rf /") is None
        assert regression_gate._safe_argv("gradle test; whoami") is None

    def test_a_nested_runner_checks_the_inner_target(self, monkeypatch):
        """``npx``/``pnpm exec`` resolve their target against the same merged set."""
        import config.sherry_settings as sherry_settings

        monkeypatch.setattr(
            sherry_settings, "load_sherry_list_setting", lambda _key: ["playwright"]
        )

        assert regression_gate._safe_argv("npx playwright test") == [
            "npx",
            "playwright",
            "test",
        ]
        assert regression_gate._safe_argv("npx definitely-not-a-verifier") is None
