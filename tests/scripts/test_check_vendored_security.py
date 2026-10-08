"""Unit tests for the vendored security backstop (audit item #58).

The scanner guards ``skills/builtin/core/multimodal_rag/scripts/graph_rag/
vendored_*`` — trees that basedpyright fully excludes and ruff only partially
covers, yet which broker API keys for several LLM providers. Tests pin both
directions: real secrets/dangerous calls are caught (with secret values never
printed), and the known non-secrets (env lookups, placeholders, token counts,
SafeLoader yaml) stay silent.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_SCANNER_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "checks" / "check_vendored_security.py"
)


@pytest.fixture(scope="module")
def scanner() -> ModuleType:
    """Load the scanner from its file path (``scripts/`` is not a package)."""
    spec = importlib.util.spec_from_file_location("check_vendored_security", _SCANNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules[cls.__module__],
    # so the module must be registered before exec_module runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(tmp_path: Path, name: str, body: str) -> Path:
    """Write a source file under ``tmp_path`` and return its path."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_flags_hardcoded_api_key_literal(scanner: ModuleType, tmp_path: Path) -> None:
    """Given a literal under an api-key name, the scan reports it without leaking the value."""
    path = _write(tmp_path, "leak.py", 'DEEPSEEK_API_KEY = "abcdef1234567890abcdef"\n')

    hits = scanner.scan_files([path], tmp_path)

    assert [hit.rule_id for hit in hits] == ["hardcoded-secret-assignment"]
    assert hits[0].rel_path == "leak.py"
    assert "abcdef1234567890abcdef" not in hits[0].display


def test_flags_known_credential_prefix(scanner: ModuleType, tmp_path: Path) -> None:
    """Given a sk- prefixed literal under a non-secret name, the prefix rule catches it."""
    path = _write(tmp_path, "leak.py", '_SIGNING_MATERIAL = "sk-probe0123456789abcdef"\n')

    hits = scanner.scan_files([path], tmp_path)

    assert [hit.rule_id for hit in hits] == ["hardcoded-secret-prefix"]


def test_flags_dangerous_calls(scanner: ModuleType, tmp_path: Path) -> None:
    """Given shell=True and eval(), the scan reports both dangerous-call rules."""
    path = _write(
        tmp_path,
        "danger.py",
        "proc = subprocess.Popen(cmd, shell=True)\nresult = eval(user_input)\n",
    )

    hits = scanner.scan_files([path], tmp_path)

    assert {hit.rule_id for hit in hits} == {"dangerous-shell-true", "dangerous-eval"}


def test_skips_env_lookups_placeholders_and_token_counts(
    scanner: ModuleType, tmp_path: Path
) -> None:
    """Given non-secret literals and config keys, the scan stays silent."""
    path = _write(
        tmp_path,
        "clean.py",
        'API_KEY = os.getenv("API_KEY")\n'
        'CLIENT_SECRET = "YOUR_CLIENT_SECRET_HERE"\n'
        'password = "changeme123"\n'
        'MAX_TOKENS = "maxTokens"\n'
        'help_text = {"max_tokens": "Maximum tokens generated per request"}\n',
    )

    assert scanner.scan_files([path], tmp_path) == []


def test_baseline_exempts_reviewed_hit(scanner: ModuleType, tmp_path: Path) -> None:
    """Given a reviewed baseline row, the matching hit becomes allowed, not violating."""
    path = _write(tmp_path, "danger.py", "proc = subprocess.Popen(cmd, shell=True)\n")
    hits = scanner.scan_files([path], tmp_path)
    assert len(hits) == 1

    without = scanner.run_scan([path], tmp_path, ())
    assert len(without.violations) == 1

    entry = scanner.BaselineEntry(
        rule_id=hits[0].rule_id,
        rel_path=hits[0].rel_path,
        fingerprint=scanner.fingerprint(hits[0].snippet),
        reason="reviewed: matches tests, not reachable at runtime",
    )
    with_baseline = scanner.run_scan([path], tmp_path, (entry,))

    assert with_baseline.violations == ()
    assert len(with_baseline.allowed) == 1
    assert with_baseline.stale == ()


def test_collect_files_skips_missing_paths(scanner: ModuleType, tmp_path: Path) -> None:
    """Given a path deleted from the change set, collection returns nothing."""
    assert scanner.collect_files([tmp_path / "gone.py"]) == []


def test_load_baseline_roundtrip_and_malformed(scanner: ModuleType, tmp_path: Path) -> None:
    """Given a well-formed TSV the parser returns entries; malformed rows raise."""
    good = tmp_path / "good.txt"
    good.write_text(
        "# comment\n\ndangerous-shell-true\tdanger.py\tabc123\treviewed for tests\n",
        encoding="utf-8",
    )
    assert scanner.load_baseline(good) == (
        scanner.BaselineEntry("dangerous-shell-true", "danger.py", "abc123", "reviewed for tests"),
    )

    bad = tmp_path / "bad.txt"
    bad.write_text("only\ttwo-fields\n", encoding="utf-8")
    with pytest.raises(ValueError):
        scanner.load_baseline(bad)


def test_main_reports_violation_with_exit_code(
    scanner: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Given a violating file, the CLI prints the finding and exits 1."""
    path = _write(tmp_path, "danger.py", "proc = subprocess.Popen(cmd, shell=True)\n")

    assert scanner.main([str(path)]) == 1
    assert "dangerous-shell-true" in capsys.readouterr().out


def test_vendored_trees_clean_under_committed_baseline(scanner: ModuleType) -> None:
    """Given the committed baseline, the vendored trees have no un-baselined hits."""
    files = scanner.collect_files(scanner.VENDORED_ROOTS)
    assert files, "vendored roots resolved to an empty scan set"

    result = scanner.run_scan(
        files, scanner.REPO_ROOT, scanner.load_baseline(scanner.BASELINE_PATH)
    )

    assert result.violations == (), [
        f"{hit.rel_path}:{hit.line}:{hit.rule_id}" for hit in result.violations
    ]
