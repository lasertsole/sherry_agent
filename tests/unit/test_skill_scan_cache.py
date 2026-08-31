"""Unit tests for the verdict-level, content-addressed disk cache in
server/service/skill_scanner.py.

All scans are faked: ``_run_cli`` is patched and the CLI ``--version`` probe
is stubbed (``shutil.which`` + ``subprocess.run``), so the real
``skillspector`` binary is never invoked and ``agent.core`` is never
imported. The tests exercise:

* the content-addressed cache key (content-only, path-free, pycache-skipped);
* cache hit/miss behaviour (warm hits, content/version/LLM-mode invalidation);
* cache policy (DO_NOT_INSTALL cached, UNAVAILABLE never stored, corrupt
  files and failed writes fail open, reset_scan_cache forces a re-scan).
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import server.service.skill_scanner as ss
from server.service.skill_scanner import (
    ScanFinding,
    ScanResult,
    ScanStatus,
    Severity,
    _scan_cache_key,
    reset_backend_cache,
    reset_scan_cache,
    scan_skill,
)

MODULE = "server.service.skill_scanner"


@pytest.fixture(autouse=True)
def _scanner_gate_open(tmp_path):
    """Deterministic isolation: gate open, fake CLI probe, tmp cache, LLM-off."""
    with (
        patch(f"{MODULE}._ENABLED_ENV", True),
        patch(f"{MODULE}._CACHE_PATH", tmp_path / "skills_scan_cache.json"),
        patch(
            f"{MODULE}.shutil.which",
            return_value=str(tmp_path / "fake_skillspector.exe"),
        ),
        patch(
            f"{MODULE}.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="SkillSpector v-fake-1"),
        ),
        patch(f"{MODULE}._llm_env", return_value={}),
    ):
        reset_backend_cache()
        reset_scan_cache()
        yield


_SKILL_MD = "---\nname: demo_skill\n---\n\nSome skill body.\n"


def _skill_dir(tmp_path: Path, name: str = "demo_skill") -> Path:
    """Create a minimal skill dir with FIXED SKILL.md content.

    The content must not depend on *name*, so two dirs with different
    directory names are byte-identical (the path must never leak into the
    cache key).
    """
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    return skill_dir


def _scanned(rec: str, score: int = 30, findings=None, backend: str = "cli") -> ScanResult:
    return ScanResult(
        status=ScanStatus.SCANNED,
        risk_score=score,
        risk_recommendation=rec,
        backend=backend,
        findings=findings or [],
    )


class TestScanCacheKey:
    def test_same_content_different_dirs_share_key(self, tmp_path):
        dir_a = _skill_dir(tmp_path, name="alpha_skill")
        dir_b = _skill_dir(tmp_path, name="beta_skill")
        key_a = _scan_cache_key(dir_a, "cli")
        key_b = _scan_cache_key(dir_b, "cli")
        assert key_a == key_b
        assert len(key_a) == 64  # sha256 hex digest

    def test_content_change_changes_key(self, tmp_path):
        skill_dir = _skill_dir(tmp_path)
        base = _scan_cache_key(skill_dir, "cli")

        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo_skill\n---\n\nChanged body.\n", encoding="utf-8"
        )
        edited = _scan_cache_key(skill_dir, "cli")
        assert edited != base

        # Interpreter noise (__pycache__ / *.pyc) is skipped by the walker.
        pycache = skill_dir / "__pycache__"
        pycache.mkdir()
        (pycache / "x.pyc").write_bytes(b"\x00\x01")
        assert _scan_cache_key(skill_dir, "cli") == edited

        (skill_dir / "notes.txt").write_text("extra", encoding="utf-8")
        assert _scan_cache_key(skill_dir, "cli") != edited

    def test_llm_fingerprint_changes_key(self, tmp_path):
        skill_dir = _skill_dir(tmp_path)
        key_off = _scan_cache_key(skill_dir, "cli")  # fixture pins _llm_env -> {}
        with patch(f"{MODULE}._llm_env", return_value={"OPENAI_BASE_URL": "http://a"}):
            key_llm_a = _scan_cache_key(skill_dir, "cli")
        with patch(f"{MODULE}._llm_env", return_value={"OPENAI_BASE_URL": "http://b"}):
            key_llm_b = _scan_cache_key(skill_dir, "cli")
        assert len({key_off, key_llm_a, key_llm_b}) == 3


class TestScanCacheHit:
    def test_miss_then_store_then_warm_hit(self, tmp_path):
        finding = ScanFinding(title="Obfuscated code", category="suspicious", severity=Severity.HIGH)
        with patch(
            f"{MODULE}._run_cli",
            return_value=_scanned("CAUTION", 40, [finding]),
        ) as run_cli:
            first = scan_skill(_skill_dir(tmp_path))
            second = scan_skill(_skill_dir(tmp_path))
        assert run_cli.call_count == 1  # second call served from cache
        assert ss._CACHE_PATH.exists()
        raw = json.loads(ss._CACHE_PATH.read_text(encoding="utf-8"))
        assert raw["version"] == ss._CACHE_VERSION == 1
        entry = next(iter(raw["entries"].values()))
        assert entry["cached_at"]
        assert second is not first  # rebuilt from dict, not the same object
        assert second.risk_score == 40
        assert second.findings[0].severity is Severity.HIGH

    def test_content_change_forces_rescan(self, tmp_path):
        skill_dir = _skill_dir(tmp_path)
        with patch(
            f"{MODULE}._run_cli",
            side_effect=[_scanned("SAFE", 5), _scanned("DO_NOT_INSTALL", 90)],
        ) as run_cli:
            scan_skill(skill_dir)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: demo_skill\n---\n\nTotally different body.\n", encoding="utf-8"
            )
            second = scan_skill(skill_dir)
        assert run_cli.call_count == 2
        assert second.is_do_not_install is True

    def test_scanner_version_change_forces_rescan(self, tmp_path):
        skill_dir = _skill_dir(tmp_path)
        with patch(f"{MODULE}._run_cli", return_value=_scanned("SAFE", 5)) as run_cli, patch(
            f"{MODULE}._scanner_version_fingerprint", return_value="v1"
        ):
            scan_skill(skill_dir)
            with patch(f"{MODULE}._scanner_version_fingerprint", return_value="v2"):
                scan_skill(skill_dir)
        assert run_cli.call_count == 2

    def test_llm_mode_change_forces_miss(self, tmp_path):
        skill_dir = _skill_dir(tmp_path)
        with patch(f"{MODULE}._run_cli", return_value=_scanned("SAFE", 5)) as run_cli, patch(
            f"{MODULE}._llm_env", return_value={}
        ):
            scan_skill(skill_dir)
            with patch(
                f"{MODULE}._llm_env",
                return_value={"OPENAI_BASE_URL": "http://x", "OPENAI_API_KEY": "k"},
            ):
                scan_skill(skill_dir)
        assert run_cli.call_count == 2


class TestScanCachePolicy:
    def test_do_not_install_verdict_is_cached_and_served(self, tmp_path):
        with patch(
            f"{MODULE}._run_cli", return_value=_scanned("DO_NOT_INSTALL", 90)
        ) as run_cli:
            scan_skill(_skill_dir(tmp_path))
            second = scan_skill(_skill_dir(tmp_path))
        assert run_cli.call_count == 1  # DO_NOT_INSTALL is SCANNED, so cached
        assert second.is_do_not_install
        assert second.risk_score == 90

    def test_unavailable_never_cached(self, tmp_path):
        skill_dir = _skill_dir(tmp_path)
        with (
            patch(f"{MODULE}._run_cli", return_value=ss._unavailable("cli")) as run_cli,
            patch(f"{MODULE}._run_python_api", return_value=ss._unavailable("python")),
        ):
            scan_skill(skill_dir)
            scan_skill(skill_dir)
        assert run_cli.call_count == 2  # every scan is a miss
        assert not ss._CACHE_PATH.exists()  # UNAVAILABLE never stored

    def test_corrupt_cache_file_fails_open(self, tmp_path):
        ss._CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        ss._CACHE_PATH.write_text("not json{", encoding="utf-8")
        with patch(
            f"{MODULE}._run_cli", return_value=_scanned("SAFE", 5)
        ) as run_cli:
            result = scan_skill(_skill_dir(tmp_path))
        assert run_cli.call_count == 1
        assert result.risk_recommendation == "SAFE"
        # The corrupt file is repaired by the store after the scan.
        raw = json.loads(ss._CACHE_PATH.read_text(encoding="utf-8"))
        assert raw["version"] == ss._CACHE_VERSION

    def test_cache_write_failure_fails_open(self, tmp_path):
        with patch(
            f"{MODULE}._run_cli", return_value=_scanned("SAFE", 5)
        ), patch(f"{MODULE}.os.replace", side_effect=OSError("disk full")):
            result = scan_skill(_skill_dir(tmp_path))  # must not raise
        assert result.risk_recommendation == "SAFE"

    def test_reset_scan_cache_removes_file(self, tmp_path):
        with patch(
            f"{MODULE}._run_cli", return_value=_scanned("SAFE", 5)
        ) as run_cli:
            scan_skill(_skill_dir(tmp_path))
            assert ss._CACHE_PATH.exists()
            reset_scan_cache()
            assert not ss._CACHE_PATH.exists()
            scan_skill(_skill_dir(tmp_path))
        assert run_cli.call_count == 2
