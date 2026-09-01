"""Structural tests for the gh-pipeline orchestration protocol skill.

Item 4 (GitHub Pipeline / gh-pipeline) of ORCHESTRATION_PORT_PLAN.md: the port
is pure protocol layer. ``skills/gh-pipeline/SKILL.md`` must carry the four
phase workflow (repo parse, issue collection, worker spawn, PR creation), the
dual-path tooling (gh CLI primary, REST v3 fallback), the embedded worker
spawn-prompt template with its four mandatory elements, the three-row
degradation table, the --dry-run / --yes execution flags, and the risk and
rollback chapter.

Environment precondition (measured 2026-08-31): the gh CLI is NOT installed
on this machine, so the protocol must spell out both paths: path A
(winget install GitHub.cli + gh auth login) and path B (REST v3, anonymous
60 req/h on public repos, useless for private repos without a token).
"""

import shutil
from pathlib import Path

import pytest

import skills.loader as loader_module
from skills.loader import _skill_visible_to, parse_frontmatter, scan_skills

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "gh-pipeline"
SKILL_PATH = SKILL_DIR / "SKILL.md"

# skills/skills_snapshot.py marks 15_000 chars as the oversized-skill budget.
SKILL_CHAR_BUDGET = 15_000


@pytest.fixture(scope="module")
def skill_text() -> str:
    assert SKILL_PATH.exists(), f"gh-pipeline protocol skill missing: {SKILL_PATH}"
    return SKILL_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def skill_meta(skill_text: str) -> dict:
    meta = parse_frontmatter(skill_text)
    assert meta, "SKILL.md has no YAML frontmatter"
    return meta


@pytest.fixture(scope="module")
def skill_body(skill_text: str) -> str:
    """SKILL.md content below the frontmatter block."""
    assert skill_text.startswith("---"), "frontmatter block must open the file"
    return skill_text.split("---", 2)[2]


@pytest.fixture(scope="module")
def body_lines(skill_body: str) -> list[str]:
    return skill_body.splitlines()


def _assert_ordered(haystack: str, needles: list[str]) -> None:
    """Every needle must appear in haystack, in the given order."""
    cursor = 0
    for needle in needles:
        idx = haystack.find(needle, cursor)
        assert idx != -1, f"missing or out of order: {needle!r}"
        cursor = idx + len(needle)


# ---------------------------------------------------------------------------
# Loader integration
# ---------------------------------------------------------------------------


def test_loader_discovers_gh_pipeline(tmp_path, monkeypatch):
    """scan_skills() picks the skill up with the schema the loader promises."""
    skills_dir = tmp_path / "skills"
    shutil.copytree(SKILL_DIR, skills_dir / "gh-pipeline")
    monkeypatch.setattr(loader_module, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(loader_module, "ROOT_DIR", tmp_path)

    records = {s["name"]: s for s in scan_skills(use_cache=False)}

    assert "gh-pipeline" in records
    record = records["gh-pipeline"]
    assert record["location"] == "./skills/gh-pipeline/SKILL.md"
    assert record["scope"] == "main_only"
    assert record["active"] is True  # not under skills/plugins/ -> always active
    assert record["description"].strip()


def test_frontmatter_fields(skill_meta):
    assert str(skill_meta.get("name")) == "gh-pipeline"
    assert str(skill_meta.get("scope")) == "main_only"  # orchestration protocol: main only
    assert str(skill_meta.get("description", "")).strip()


def test_main_only_visibility_contract(skill_meta):
    record = {"scope": skill_meta.get("scope")}
    assert _skill_visible_to(record, "main") is True
    assert _skill_visible_to(record, "subagent") is False


def test_skill_within_char_budget(skill_text):
    assert len(skill_text) <= SKILL_CHAR_BUDGET


def test_four_phases_in_order(skill_body):
    _assert_ordered(
        skill_body,
        ["Phase 1", "Phase 2", "Phase 3", "Phase 4"],
    )


# ---------------------------------------------------------------------------
# Phase 1: repo identity parsing (plan §4.3 item 1)
# ---------------------------------------------------------------------------


def test_phase1_repo_identity_from_git_remote(skill_body):
    assert "git remote get-url origin" in skill_body
    # Both canonical URL forms must be parsed by the same regex approach.
    assert "https://github.com/<owner>/<repo>.git" in skill_body
    assert "git@github.com:<owner>/<repo>.git" in skill_body


# ---------------------------------------------------------------------------
# Phase 2: issue collection, dual path (plan §4.3 item 2)
# ---------------------------------------------------------------------------


def test_phase2_gh_cli_path(skill_body):
    _assert_ordered(
        skill_body,
        [
            "gh issue list --repo <owner>/<repo> --label <label> --limit <N> --state open --json number,title,body",
            "--milestone",
        ],
    )


def test_phase2_rest_fallback_path(skill_body):
    # REST v3 fallback when gh is unavailable.
    assert "GET /repos/{owner}/{repo}/issues?labels=<label>&per_page=<N>&state=open" in skill_body
    # Anonymous rate limit on the REST path: 60 req/h per IP.
    assert "60 req/h" in skill_body


def test_phase2_filters_out_pull_requests(skill_body):
    # Issues endpoint also returns PRs; entries with a non-empty
    # pull_request field must be filtered out.
    pr_lines = [ln for ln in skill_body.splitlines() if "pull_request" in ln]
    assert pr_lines, "the PR-entry filter must be spelled out"
    assert any("过滤" in ln or "排除" in ln for ln in pr_lines)


# ---------------------------------------------------------------------------
# Phase 3: worker spawn template, four mandatory elements (plan §4.3 item 3)
# ---------------------------------------------------------------------------


def test_phase3_spawn_template_four_elements(skill_body):
    _assert_ordered(
        skill_body,
        [
            "issue 编号",
            "issue 标题",
            "issue 正文",
            "gh-pipeline/<issue-number>",
            "tool_allow",
            "read,write,patch,python_repl,terminal",
            "tool_deny",
        ],
    )


def test_phase3_report_protocol_via_announce(skill_body):
    assert "announce" in skill_body
    _assert_ordered(
        skill_body,
        ["分支名", "变更摘要", "自验命令"],
    )


# ---------------------------------------------------------------------------
# Phase 4: PR creation, dual path (plan §4.3 item 4)
# ---------------------------------------------------------------------------


def test_phase4_gh_cli_path(skill_body):
    assert "gh pr create --repo <owner>/<repo> --head gh-pipeline/<issue-number> --base main" in skill_body


def test_phase4_rest_fallback_needs_token(skill_body):
    assert "POST /repos/{owner}/{repo}/pulls" in skill_body
    token_lines = [ln for ln in skill_body.splitlines() if "POST /repos/{owner}/{repo}/pulls" in ln]
    assert any("token" in ln.lower() for ln in token_lines), (
        "the REST PR creation path must state its token requirement"
    )


# ---------------------------------------------------------------------------
# Environment preparation (measured 2026-08-31: gh CLI absent)
# ---------------------------------------------------------------------------


def test_environment_preparation_path_a(skill_body):
    _assert_ordered(
        skill_body,
        ["winget install GitHub.cli", "gh auth login", "gh auth status"],
    )
    assert "GH_TOKEN" in skill_body


# ---------------------------------------------------------------------------
# Degradation table (plan §4.4: exactly three rows)
# ---------------------------------------------------------------------------


def test_degradation_table_three_rows(skill_body):
    # Row 1: public repo, REST anonymous quota is enough for a small batch.
    assert "公开仓" in skill_body
    # Row 2: private repo, gh CLI (or a token) is mandatory.
    assert "私有仓" in skill_body
    # Row 3: no gh and no token, only produce branches, PR handled manually.
    assert "只出分支" in skill_body
    assert "PR 手工" in skill_body


# ---------------------------------------------------------------------------
# Execution flags (plan §4.5)
# ---------------------------------------------------------------------------


def test_dry_run_flag(skill_body):
    assert "--dry-run" in skill_body
    dry_lines = [ln for ln in skill_body.splitlines() if "--dry-run" in ln]
    assert any("零远程写" in ln for ln in dry_lines), (
        "--dry-run must be defined as zero remote writes"
    )


def test_yes_flag(skill_body):
    assert "--yes" in skill_body
    yes_lines = [ln for ln in skill_body.splitlines() if "--yes" in ln]
    assert any("跳过确认" in ln for ln in yes_lines), (
        "--yes must be defined as skipping the confirmation step"
    )


# ---------------------------------------------------------------------------
# Risks and rollback (plan §4.6)
# ---------------------------------------------------------------------------


def test_risk_token_never_persisted_or_echoed(skill_body):
    token_risk_lines = [ln for ln in skill_body.splitlines() if "token" in ln.lower()]
    assert any("落盘" in ln for ln in token_risk_lines), "token must never hit disk"
    assert any("echo" in ln.lower() for ln in token_risk_lines), "token must never be echoed"


def test_risk_rate_limit_no_retry_storm(skill_body):
    rate_lines = [ln for ln in skill_body.splitlines() if ("403" in ln or "429" in ln)]
    assert rate_lines, "403/429 handling must be spelled out"
    assert any("中止" in ln or "暂停" in ln for ln in rate_lines), (
        "rate-limit handling must abort or pause the batch"
    )
    assert "重试风暴" in skill_body or "不重试" in skill_body


def test_gate_missing_selfverify_blocks_phase4(skill_body):
    gate_lines = [ln for ln in skill_body.splitlines() if "自验" in ln]
    assert any("不进入 Phase 4" in ln for ln in gate_lines), (
        "workers without self-verification output must not reach Phase 4"
    )


def test_rest_v3_version_note(skill_body):
    note_lines = [ln for ln in skill_body.splitlines() if "2026-08" in ln]
    assert note_lines, "the REST v3 API reference must carry its access date"


def test_rollback(skill_body):
    rb_lines = [ln for ln in skill_body.splitlines() if "回滚" in ln]
    assert rb_lines, "the rollback chapter must exist"
    assert any("删除" in ln for ln in rb_lines)
