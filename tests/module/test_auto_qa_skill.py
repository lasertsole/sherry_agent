"""Unit tests for the auto-qa protocol skill (ORCHESTRATION_PORT_PLAN ch.2).

Pure protocol-layer tests: they only assert on the markdown artifacts under
skills/auto-qa/ plus skills.loader discovery / visibility semantics. No
runtime code is imported or exercised. All scan_skills calls use
use_cache=False so a stale skills/skills_snapshot.json can never mask the
new skill (verified fact: scan_skills(use_cache=True) prefers the snapshot,
loader.py:82-89).

Coverage:
* discovery: scan_skills(use_cache=False) finds name=auto-qa, scope=main_only;
* visibility: loader.py:63-79 contract, main sees it, subagent does not;
* SKILL.md body: campaign goal, wave state machine, lane partition,
  dispatch template, evidence ledger, capacity throttle, explicit
  precondition declaration;
* references/roles.md: read-only reviewer template (tool_deny list, fixed
  report format, input constraints) and writable fixer template (allowed
  tools, self-verification, root-cause discipline).
"""

from pathlib import Path
from typing import Any

import skills.loader as loader_module
from skills.loader import _skill_visible_to, parse_frontmatter, scan_skills

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_MD = REPO_ROOT / "skills" / "auto-qa" / "SKILL.md"
ROLES_MD = REPO_ROOT / "skills" / "auto-qa" / "references" / "roles.md"

WAVE_STATES = ("dispatching", "collecting", "reviewing", "done")
LEDGER_FIELDS = ("PID", "baseline SHA", "复现命令", "验证输出", "评审结论")


def _skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _roles_text() -> str:
    return ROLES_MD.read_text(encoding="utf-8")


def _scanned_entry() -> dict[str, Any]:
    entries = {s["name"]: s for s in scan_skills(use_cache=False)}
    assert "auto-qa" in entries, (
        f"auto-qa not discovered by scan_skills(use_cache=False); got: {sorted(entries)}"
    )
    return entries["auto-qa"]


class TestSkillDiscovery:
    def test_scan_without_cache_finds_auto_qa_main_only(self):
        entry = _scanned_entry()
        assert entry["scope"] == "main_only"
        assert entry["location"] == "./skills/auto-qa/SKILL.md"
        # Builtin (not under skills/plugins/), therefore always active.
        assert entry["active"] is True

    def test_frontmatter_parseable_via_loader(self):
        meta = parse_frontmatter(_skill_text())
        assert meta.get("name") == "auto-qa"
        assert meta.get("scope") == "main_only"
        assert str(meta.get("description", "")).strip() != ""

    def test_description_is_single_line(self):
        meta = parse_frontmatter(_skill_text())
        desc = str(meta.get("description", ""))
        assert "\n" not in desc

    def test_loader_normalize_scope_keeps_main_only(self):
        assert loader_module._normalize_scope("main_only") == "main_only"


class TestVisibilityContract:
    """Visibility semantics per skills/loader.py:63-79."""

    def test_scanned_entry_visible_to_main_not_subagent(self):
        entry = _scanned_entry()
        assert _skill_visible_to(entry, "main") is True
        assert _skill_visible_to(entry, "subagent") is False

    def test_helper_semantics_for_main_only_scope(self):
        skill = {"scope": "main_only"}
        assert _skill_visible_to(skill, "main") is True
        assert _skill_visible_to(skill, "subagent") is False


class TestSkillBodyCampaign:
    def test_campaign_goal(self):
        text = _skill_text()
        assert "自动修 QA 失败项" in text
        assert "100 个 verified fixes" in text
        assert "证据台账" in text
        # Counting only accepts qualified ledger entries; self-claims never count.
        assert "自称修完不计入" in text


class TestSkillBodyWaveStateMachine:
    def test_four_states_present(self):
        text = _skill_text()
        for state in WAVE_STATES:
            assert state in text, f"missing wave state: {state}"

    def test_state_file_path_and_owner(self):
        text = _skill_text()
        assert ".omo/evidence/auto-qa/<wave>/wave-state.md" in text
        assert "主会话维护" in text

    def test_retro_gate_blocks_next_wave(self):
        text = _skill_text()
        assert "复盘未完成" in text
        assert "下一波" in text


class TestSkillBodyLanePartition:
    def test_lane_examples_and_group_mapping(self):
        text = _skill_text()
        for example in ("auth", "api", "ui"):
            assert example in text, f"missing lane example: {example}"
        assert "一一对应" in text

    def test_group_limits_match_swarm_types(self):
        text = _skill_text()
        # agent/tools/subagent/types/swarm.py:27
        assert "max_children_per_group=5" in text
        # agent/tools/subagent/types/swarm.py:29
        assert "max_concurrent=3" in text

    def test_group_count_formula(self):
        text = _skill_text()
        assert "ceil(N/5)" in text

    def test_oversubscription_stays_reserved(self):
        text = _skill_text()
        assert "RESERVED" in text
        assert "波内不追加派发" in text


class TestSkillBodyDispatchTemplate:
    def test_sessions_spawn_sample(self):
        text = _skill_text()
        assert "sessions_spawn" in text
        assert 'task_name="autoqa-w<n>-<lane>"' in text
        assert 'mode="run"' in text
        assert 'cleanup="keep"' in text


class TestSkillBodyEvidenceLedger:
    def test_ledger_path(self):
        text = _skill_text()
        assert ".omo/evidence/auto-qa/<wave>/<fix-id>.md" in text

    def test_five_mandatory_fields(self):
        text = _skill_text()
        for field in LEDGER_FIELDS:
            assert field in text, f"missing ledger field: {field}"

    def test_admission_rule(self):
        text = _skill_text()
        assert "PID 与 baseline SHA 缺一即不计入" in text


class TestSkillBodyCapacityThrottle:
    def test_grouped_dispatch_algorithm(self):
        text = _skill_text()
        assert "分组派发" in text
        assert "max_concurrent=3" in text

    def test_retro_checks_ledger_completeness_first(self):
        text = _skill_text()
        assert "台账完整性" in text


class TestSkillBodyPrecondition:
    def test_explicit_dependency_on_spawn_schema_extension(self):
        text = _skill_text()
        assert "tool_allow" in text
        assert "tool_deny" in text
        assert "SUBAGENT_PORT_PLAN 2.2" in text

    def test_degraded_review_path(self):
        text = _skill_text()
        assert "降级" in text
        assert "代查" in text


class TestRolesReviewerTemplate:
    def test_tool_deny_list(self):
        text = _roles_text()
        assert "tool_deny" in text
        for tool in ("write", "patch", "edit"):
            assert tool in text, f"reviewer tool_deny missing: {tool}"

    def test_fixed_report_format(self):
        text = _roles_text()
        for section in ("结论", "根因", "验证命令", "是否可修"):
            assert section in text, f"reviewer report missing section: {section}"

    def test_result_written_back_to_review_field(self):
        text = _roles_text()
        assert "review" in text
        assert "字段" in text

    def test_input_excludes_fix_diff(self):
        text = _roles_text()
        assert "不含修复 diff" in text
        assert "frozen baseline" in text
        assert "失败现象" in text


class TestRolesFixerTemplate:
    def test_allowed_tools(self):
        text = _roles_text()
        assert "write、patch、terminal" in text

    def test_self_verification_output_pasted_verbatim(self):
        text = _roles_text()
        assert "自验" in text
        assert "原文" in text

    def test_root_cause_discipline(self):
        text = _roles_text()
        assert "根因重构" in text
        assert "无关重构" in text
        assert "表面补丁" in text


class TestStyleConstraints:
    def test_no_em_dash_in_skill_files(self):
        for path, text in ((SKILL_MD, _skill_text()), (ROLES_MD, _roles_text())):
            assert "\u2014" not in text, f"em dash found in {path}"
