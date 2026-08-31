"""Unit tests for server/service/skill_scanner.py — SkillSpector wrapper.

These tests stub the outermost functions (``_resolve_backend`` and the
invocation subroutines) so that no external ``skillspector`` dependency is
required. They exercise:

* backend resolution (CLI-first, then python-API, then unavailable);
* CLI exit-code handling (0 = SAFE/CAUTION, 1 = DO_NOT_INSTALL, 2 = error);
* Python-API fallback when the CLI errors;
* graceful degradation to UNAVAILABLE when no backend exists;
* the scan_skill() file-vs-directory path normalisation;
* the _scanner_reject_message() policy (fail-closed on DO_NOT_INSTALL,
  allow on CAUTION/SAFE/UNAVAILABLE).
"""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from server.service.skill_scanner import (
    ScanResult,
    ScanStatus,
    build_caution_warnings,
    build_reject_message,
    _resolve_backend,
    _extract_scan_result,
    _normalise_findings,
    _llm_env,
    _cli_timeout,
    _CLI_TIMEOUT,
    scan_skill,
)


@pytest.fixture(autouse=True)
def _scanner_gate_open():
    """Immunity to ambient SKILL_SCANNER_ENABLED: tests control the gate via
    attribute patches; the runner sets the env var to 0, which would otherwise
    short-circuit _resolve_backend before patched backends are reached."""
    with patch("server.service.skill_scanner._ENABLED_ENV", True):
        yield


def _file_skill_dir(tmp_path: Path) -> Path:
    """Create a minimal skill dir containing SKILL.md and return the SKILL.md."""
    skill_dir = tmp_path / "demo_skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("---\nname: demo_skill\n---\n\nSome skill body.\n", encoding="utf-8")
    return skill_md


def _scanned(rec: str, score: int = 30, findings=None, backend: str = "cli") -> ScanResult:
    return ScanResult(
        status=ScanStatus.SCANNED,
        risk_score=score,
        risk_recommendation=rec,
        backend=backend,
        findings=findings or [],
    )


class TestBackendResolution:
    def test_cli_first_when_present(self):
        with patch(
            "server.service.skill_scanner._probe_backend",
            side_effect=lambda b: b == "cli",
        ) as probe:
            assert _resolve_backend() == "cli"
            # CLI is probed first and short-circuits; python is never reached.
            assert [c.args for c in probe.call_args_list] == [("cli",)]

    def test_python_fallback_when_cli_absent(self):
        with patch(
            "server.service.skill_scanner._probe_backend",
            side_effect=lambda b: b == "python",
        ) as probe:
            assert _resolve_backend() == "python"
            probe.assert_any_call("cli")
            probe.assert_any_call("python")

    def test_none_when_both_absent(self):
        with patch(
            "server.service.skill_scanner._probe_backend",
            return_value=False,
        ):
            assert _resolve_backend() is None

    def test_returns_none_when_disabled(self):
        with patch("server.service.skill_scanner._ENABLED_ENV", False):
            # Explicitly bypass the auto-probing.
            assert _resolve_backend() is None


class TestScanSkill:
    def test_missing_path_is_unavailable(self):
        with patch("server.service.skill_scanner._resolve_backend", return_value=None):
            result = scan_skill("definitely/not/a/real/path")
        assert result.is_unavailable

    def test_file_path_lifts_to_parent_dir(self, tmp_path):
        skill_md = _file_skill_dir(tmp_path)
        with (
            patch("server.service.skill_scanner._resolve_backend", return_value="cli"),
            patch("server.service.skill_scanner._run_cli") as run_cli,
        ):
            run_cli.return_value = _scanned("SAFE", score=5)
            result = scan_skill(str(skill_md))
        # The scan must target the parent directory, not the SKILL.md file.
        run_cli.assert_called_once()
        args = run_cli.call_args[0][0]
        assert args == skill_md.parent
        assert result.risk_recommendation == "SAFE"

    def test_dir_path_passed_through(self, tmp_path):
        skill_dir = tmp_path / "demo_skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("content", encoding="utf-8")
        with (
            patch("server.service.skill_scanner._resolve_backend", return_value="cli"),
            patch("server.service.skill_scanner._run_cli") as run_cli,
        ):
            run_cli.return_value = _scanned("CAUTION", score=40)
            scan_skill(str(skill_dir))
        run_cli.assert_called_once()
        assert run_cli.call_args[0][0] == skill_dir

    def test_unavailable_when_no_backend(self, tmp_path):
        skill_md = _file_skill_dir(tmp_path)
        with patch("server.service.skill_scanner._resolve_backend", return_value=None):
            result = scan_skill(skill_md)
        assert result.is_unavailable
        assert result.backend is None

    def test_cli_backend_selected(self, tmp_path):
        skill_md = _file_skill_dir(tmp_path)
        with (
            patch("server.service.skill_scanner._resolve_backend", return_value="cli"),
            patch("server.service.skill_scanner._run_cli") as run_cli,
        ):
            run_cli.return_value = _scanned("SAFE")
            result = scan_skill(skill_md)
        assert result.backend == "cli"

    def test_python_backend_selected(self, tmp_path):
        skill_md = _file_skill_dir(tmp_path)
        with (
            patch("server.service.skill_scanner._resolve_backend", return_value="python"),
            patch("server.service.skill_scanner._run_python_api") as run_py,
        ):
            run_py.return_value = _scanned("SAFE", backend="python")
            result = scan_skill(skill_md)
        assert result.backend == "python"


class TestCliSubprocess:
    def test_ok_exit_parses_payload(self, tmp_path):
        payload = '{"risk_score": 10, "risk_recommendation": "SAFE", "risk_severity": "low", "filtered_findings": []}'
        proc = MagicMock(returncode=0, stdout=payload, stderr="")
        with (
            patch(
                "server.service.skill_scanner._probe_backend",
                lambda b: b == "cli",
            ),
            patch("server.service.skill_scanner.shutil.which", return_value="/x/skillspector"),
            patch("server.service.skill_scanner.subprocess.run", return_value=proc) as run,
        ):
            result = scan_skill(tmp_path)
        run.assert_called_once()
        result.risk_score == 10
        assert result.risk_recommendation == "SAFE"
        assert result.backend == "cli"

    def test_do_not_install_exit_forces_verdict(self, tmp_path):
        # exit code 1 = DO_NOT_INSTALL even if the body is inconsistent.
        payload = '{"risk_score": 90, "risk_recommendation": "SAFE", "risk_severity": "high"}'
        proc = MagicMock(returncode=1, stdout=payload, stderr="")
        with (
            patch(
                "server.service.skill_scanner._probe_backend",
                lambda b: b == "cli",
            ),
            patch("server.service.skill_scanner.shutil.which", return_value="/x/skillspector"),
            patch("server.service.skill_scanner.subprocess.run", return_value=proc),
        ):
            result = scan_skill(tmp_path)
        assert result.is_do_not_install
        assert "DO_NOT_INSTALL" in (result.risk_recommendation or "")

    def test_error_exit_falls_back_to_python_api(self, tmp_path):
        # CLI exit code 2 = error → fall through to the python API.
        proc_err = MagicMock(returncode=2, stdout="", stderr="boom")
        with (
            patch(
                "server.service.skill_scanner._probe_backend",
                lambda b: b == "cli",
            ),
            patch("server.service.skill_scanner.shutil.which", return_value="/x/skillspector"),
            patch(
                "server.service.skill_scanner.subprocess.run",
                return_value=proc_err,
            ) as run,
            patch(
                "server.service.skill_scanner._run_python_api",
            ) as run_py,
        ):
            run_py.return_value = _scanned("SAFE", score=0, backend="python")
            result = scan_skill(tmp_path)
        run.assert_called_once()
        run_py.assert_called_once()
        assert result.backend == "python"
        assert result.status is ScanStatus.SCANNED  # fell back and produced a verdict

    def test_no_cli_binary_returns_unavailable(self, tmp_path):
        with (
            patch(
                "server.service.skill_scanner._probe_backend",
                lambda b: b == "cli",
            ),
            patch("server.service.skill_scanner.shutil.which", return_value=None),
        ):
            result = scan_skill(tmp_path)
        assert result.is_unavailable

    def test_timeout_returns_unavailable(self, tmp_path):
        import subprocess as real_subprocess

        with (
            patch(
                "server.service.skill_scanner._probe_backend",
                lambda b: b == "cli",
            ),
            patch("server.service.skill_scanner.shutil.which", return_value="/x/skillspector"),
            patch(
                "server.service.skill_scanner.subprocess.run",
                side_effect=real_subprocess.TimeoutExpired(cmd=["skillspector"], timeout=1),
            ),
        ):
            result = scan_skill(tmp_path)
        assert result.is_unavailable


class TestExtractScanResult:
    def test_extracts_findings(self):
        payload = {
            "risk_score": 80,
            "risk_recommendation": "DO_NOT_INSTALL",
            "risk_severity": "critical",
            "filtered_findings": [
                {"title": "Prompt injection", "category": "injection", "severity": "critical"},
                {"title": "Exfil", "category": "data", "severity": "high"},
            ],
        }
        r = _extract_scan_result(payload, backend="cli")
        assert r.risk_score == 80
        assert r.risk_recommendation == "DO_NOT_INSTALL"
        assert r.backend == "cli"
        assert len(r.findings) == 2
        assert r.findings[0].title == "Prompt injection"
        assert r.findings[0].severity.value == "critical"

    def test_missing_fields_default(self):
        r = _extract_scan_result({}, backend="python")
        assert r.risk_score == 0
        assert r.risk_recommendation == "UNKNOWN"
        assert r.backend == "python"

    def test_none_payload(self):
        r = _extract_scan_result(None, backend="cli")
        assert r.risk_score == 0

    def test_bad_score_defaults_to_zero(self):
        r = _extract_scan_result({"risk_score": "not-a-number"}, backend="cli")
        assert r.risk_score == 0


class TestNormaliseFindings:
    def test_flat_list(self):
        out = _normalise_findings(
            [
                {"title": "one", "severity": "high"},
                {"title": "two"},
            ]
        )
        assert len(out) == 2
        assert out[0].title == "one"
        assert out[0].severity.value == "high"
        assert out[1].severity.value in ("low", "medium", "high", "critical")

    def test_nested_container(self):
        out = _normalise_findings({"findings": [{"title": "nested", "severity": "critical"}]})
        assert len(out) == 1
        assert out[0].title == "nested"

    def test_non_dict_items_skipped(self):
        out = _normalise_findings(["garbage", {"title": "ok"}])
        assert len(out) == 1


class TestRejectMessagePolicy:
    def test_do_not_install_rejects(self):
        result = _scanned(
            "DO_NOT_INSTALL", score=90, findings=[type("F", (), {"title": "data exfiltration"})()]
        )
        msg = build_reject_message(result)
        assert msg is not None
        assert "DO_NOT_INSTALL" in msg
        assert "90" in msg

    def test_caution_allows(self):
        result = _scanned("CAUTION", score=40)
        assert build_reject_message(result) is None

    def test_safe_allows(self):
        result = _scanned("SAFE", score=5)
        assert build_reject_message(result) is None

    def test_unavailable_allows(self):
        result = ScanResult(status=ScanStatus.UNAVAILABLE)
        assert build_reject_message(result) is None


class TestCautionWarnings:
    def test_caution_with_findings(self):
        result = _scanned(
            "CAUTION",
            score=40,
            findings=[
                type("F", (), {"title": "crypto usage"})(),
                type("F", (), {"title": "obfuscated script"})(),
            ],
        )
        warnings = build_caution_warnings(result)
        assert len(warnings) == 1
        assert "CAUTION" in warnings[0]
        assert "40" in warnings[0]
        assert "crypto usage" in warnings[0]
        assert "obfuscated script" in warnings[0]

    def test_caution_without_findings(self):
        result = _scanned("CAUTION", score=25)
        warnings = build_caution_warnings(result)
        assert len(warnings) == 1
        assert "CAUTION" in warnings[0]
        assert "25" in warnings[0]

    def test_caution_dedupes_repeated_finding_titles(self):
        result = _scanned(
            "CAUTION",
            score=30,
            findings=[
                type("F", (), {"title": "eval usage"})(),
                type("F", (), {"title": "eval usage"})(),
            ],
        )
        warnings = build_caution_warnings(result)
        assert warnings[0].count("eval usage") == 1

    def test_safe_produces_no_warnings(self):
        assert build_caution_warnings(_scanned("SAFE", score=5)) == []

    def test_do_not_install_produces_no_warnings(self):
        # DO_NOT_INSTALL is rejected via build_reject_message instead.
        result = _scanned(
            "DO_NOT_INSTALL", score=90, findings=[type("F", (), {"title": "data exfiltration"})()]
        )
        assert build_caution_warnings(result) == []

    def test_unavailable_produces_no_warnings(self):
        result = ScanResult(status=ScanStatus.UNAVAILABLE)
        assert build_caution_warnings(result) == []


class TestScanResultPredicates:
    def test_is_do_not_install(self):
        assert _scanned("DO_NOT_INSTALL").is_do_not_install
        assert not _scanned("CAUTION").is_do_not_install
        assert not _scanned("SAFE").is_do_not_install

    def test_is_caution(self):
        assert _scanned("CAUTION").is_caution
        assert not _scanned("DO_NOT_INSTALL").is_caution

    def test_is_unavailable_not_scanned(self):
        r = ScanResult(status=ScanStatus.UNAVAILABLE)
        assert r.is_unavailable
        assert not r.is_do_not_install
        assert not r.is_caution

    def test_to_dict(self):
        result = _scanned(
            "CAUTION",
            score=35,
            findings=[type("F", (), {"to_dict": lambda self: {"title": "x"}})()],
        )
        d = result.to_dict()
        assert d["status"] == "scanned"
        assert d["risk_score"] == 35
        assert d["risk_recommendation"] == "CAUTION"
        assert d["backend"] == "cli"
        assert d["findings"][0]["title"] == "x"


# ---------------------------------------------------------------------------
# _llm_env / _cli_timeout — LLM opt-in flag + operator timeout override
# ---------------------------------------------------------------------------

#: Env vars scrubbed before every _llm_env/_cli_timeout test. The ambient
#: machine .env sets AUXILIARY_LLM_* (and operator shells may export the
#: OPENAI_*/SKILLSPECTOR_* overrides), so each test starts from a clean slate.
_LLM_ENV_SCRUB = (
    "AUXILIARY_LLM_API_BASE",
    "AUXILIARY_LLM_API_KEY",
    "AUXILIARY_LLM_API_NAME",
    "SKILL_SCANNER_LLM",
    "SKILL_SCANNER_TIMEOUT",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "SKILLSPECTOR_PROVIDER",
    "SKILLSPECTOR_MODEL",
)


def _scrub_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _LLM_ENV_SCRUB:
        monkeypatch.delenv(name, raising=False)


class TestLlmEnv:
    def test_flag_unset_returns_empty_even_with_creds(self, monkeypatch):
        _scrub_llm_env(monkeypatch)
        # SKILL_SCANNER_LLM defaults to OFF. _LLM_ENABLED_ENV is read once at
        # import time, so the flag's effect is pinned via the module constant
        # (an env-only setenv cannot rebind it).
        monkeypatch.setattr("server.service.skill_scanner._LLM_ENABLED_ENV", False)
        monkeypatch.setenv("AUXILIARY_LLM_API_BASE", "https://api.example.com/v1")
        monkeypatch.setenv("AUXILIARY_LLM_API_KEY", "sk-test")
        assert _llm_env() == {}

    def test_flag_on_with_creds_forwards_env(self, monkeypatch):
        _scrub_llm_env(monkeypatch)
        monkeypatch.setattr("server.service.skill_scanner._LLM_ENABLED_ENV", True)
        monkeypatch.setenv("SKILL_SCANNER_LLM", "1")
        monkeypatch.setenv("AUXILIARY_LLM_API_BASE", "https://api.example.com/v1")
        monkeypatch.setenv("AUXILIARY_LLM_API_KEY", "sk-test")
        monkeypatch.setenv("AUXILIARY_LLM_API_NAME", "glm-test")
        assert _llm_env() == {
            "SKILLSPECTOR_PROVIDER": "openai",
            "OPENAI_BASE_URL": "https://api.example.com/v1",
            "OPENAI_API_KEY": "sk-test",
            "SKILLSPECTOR_MODEL": "glm-test",
        }

    def test_flag_on_without_creds_returns_empty(self, monkeypatch):
        _scrub_llm_env(monkeypatch)
        monkeypatch.setattr("server.service.skill_scanner._LLM_ENABLED_ENV", True)
        monkeypatch.setenv("SKILL_SCANNER_LLM", "1")
        assert _llm_env() == {}


class TestCliTimeout:
    def test_unset_returns_default(self, monkeypatch):
        _scrub_llm_env(monkeypatch)
        assert _CLI_TIMEOUT == 120
        assert _cli_timeout() == 120

    def test_valid_override(self, monkeypatch):
        _scrub_llm_env(monkeypatch)
        monkeypatch.setenv("SKILL_SCANNER_TIMEOUT", "300")
        assert _cli_timeout() == 300

    def test_zero_clamped_to_one(self, monkeypatch):
        _scrub_llm_env(monkeypatch)
        monkeypatch.setenv("SKILL_SCANNER_TIMEOUT", "0")
        assert _cli_timeout() == 1

    def test_negative_clamped_to_one(self, monkeypatch):
        _scrub_llm_env(monkeypatch)
        monkeypatch.setenv("SKILL_SCANNER_TIMEOUT", "-5")
        assert _cli_timeout() == 1

    def test_invalid_falls_back_to_default(self, monkeypatch):
        _scrub_llm_env(monkeypatch)
        monkeypatch.setenv("SKILL_SCANNER_TIMEOUT", "abc")
        assert _cli_timeout() == 120
