"""Structural tests for the repair-sweep orchestration protocol skill.

Item 3 (修复清扫舰队 / Repair Sweep) of ORCHESTRATION_PORT_PLAN.md: the port is
pure protocol layer. ``skills/repair-sweep/SKILL.md`` must carry the parameter
model (scope tiers / batch_size cap / worker fleet sizing), the six-step fleet
workflow, the embedded worker spawn-prompt template with the trimmed
authorization list (land/close require orchestrator review), the worktree
lifecycle rules, and the challenge + kill fallback channels.

These tests lock that protocol surface in -- the protocol-testable subset of
the plan's acceptance criteria (§3.5: batch cap rejection, challenge trail,
worktree cleanup verification, authorization list in every spawn prompt,
queue slicing without overlap).
"""

import shutil
from pathlib import Path

import pytest

import skills.loader as loader_module
from skills.loader import _skill_visible_to, parse_frontmatter, scan_skills

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "skills" / "repair-sweep"
SKILL_PATH = SKILL_DIR / "SKILL.md"

# skills/skills_snapshot.py marks 15_000 chars as the oversized-skill budget.
SKILL_CHAR_BUDGET = 15_000


@pytest.fixture(scope="module")
def skill_text() -> str:
    assert SKILL_PATH.exists(), f"repair-sweep protocol skill missing: {SKILL_PATH}"
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


def test_loader_discovers_repair_sweep(tmp_path, monkeypatch):
    """scan_skills() picks the skill up with the schema the loader promises."""
    skills_dir = tmp_path / "skills"
    shutil.copytree(SKILL_DIR, skills_dir / "repair-sweep")
    monkeypatch.setattr(loader_module, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(loader_module, "ROOT_DIR", tmp_path)

    records = {s["name"]: s for s in scan_skills(use_cache=False)}

    assert "repair-sweep" in records
    record = records["repair-sweep"]
    assert record["location"] == "./skills/repair-sweep/SKILL.md"
    assert record["scope"] == "main_only"
    assert record["active"] is True  # not under skills/plugins/ -> always active
    assert record["description"].strip()


def test_frontmatter_fields(skill_meta):
    assert str(skill_meta.get("name")) == "repair-sweep"
    assert str(skill_meta.get("scope")) == "main_only"  # orchestration protocol: main only
    assert str(skill_meta.get("description", "")).strip()


def test_main_only_visibility_contract(skill_meta):
    record = {"scope": skill_meta.get("scope")}
    assert _skill_visible_to(record, "main") is True
    assert _skill_visible_to(record, "subagent") is False


def test_skill_within_char_budget(skill_text):
    assert len(skill_text) <= SKILL_CHAR_BUDGET


# ---------------------------------------------------------------------------
# Parameter chapter (plan §3.4 item 1)
# ---------------------------------------------------------------------------


def test_scope_has_three_tiers(body_lines, skill_body):
    refs_lines = [ln for ln in body_lines if "refs" in ln]
    assert any(("反查" in ln or "引用" in ln) for ln in refs_lines), "refs tier undefined"

    assert "默认档" in skill_body, "discovery must be declared the default tier"
    disc_lines = [ln for ln in body_lines if "discovery" in ln]
    assert any("默认" in ln for ln in disc_lines)

    queue_lines = [ln for ln in body_lines if "queue" in ln]
    assert any(".omo/repair-sweep/queue.md" in ln for ln in queue_lines), (
        "queue tier must name its external queue file"
    )


def test_batch_size_default_cap_and_rejection(skill_body):
    assert "batch_size" in skill_body
    assert "默认 5" in skill_body
    assert "上限 20" in skill_body
    # Over-cap request: reject the whole round, no dispatch at all.
    assert "超过 20" in skill_body
    assert "拒绝" in skill_body and "拆批" in skill_body


def test_worker_fleet_sizing_and_swarm_limits(skill_body):
    # Defaults per scope tier: refs 8 / discovery 8 / queue 64.
    assert "refs 8 / discovery 8 / queue 64" in skill_body
    # Swarm capacity contract: 5 workers per group, 3 concurrent per group.
    assert "max_children_per_group" in skill_body
    assert "max_concurrent" in skill_body
    assert "每 5 workers 一组" in skill_body


# ---------------------------------------------------------------------------
# Six-step workflow (plan §3.4 item 1, 流程章)
# ---------------------------------------------------------------------------


def test_six_step_workflow_in_order(skill_body):
    _assert_ordered(
        skill_body,
        [
            "队列构建",
            "批次切片",
            "spawn",
            "收集",
            "质疑处理",
            "汇总与 worktree 清理",
        ],
    )


def test_batch_slicing_rules(skill_body):
    # File exclusivity inside one round: the same file never enters two workers.
    assert "同一文件不进两个 worker" in skill_body
    # queue tier: batch count = ceil(entries / batch_size), slices never overlap.
    assert "ceil(条目数 / batch_size)" in skill_body
    assert "无重叠" in skill_body


def test_dispatch_and_collection_channels(skill_body):
    assert "sessions_spawn" in skill_body, "workers are dispatched via sessions_spawn"
    assert "announce" in skill_body, "results flow back through the announce pipeline"


# ---------------------------------------------------------------------------
# Worker spawn-prompt template (plan §3.4 item 2)
# ---------------------------------------------------------------------------


def test_worker_authorization_list(skill_body):
    # Directly granted actions, in the template's own order.
    _assert_ordered(skill_body, ["investigate", "fix", "commit", "push", "PR", "comment"])
    # land / close are trimmed: they require orchestrator review before running.
    land_lines = [ln for ln in skill_body.splitlines() if "land" in ln]
    close_lines = [ln for ln in skill_body.splitlines() if "close" in ln]
    assert any("复核" in ln for ln in land_lines), "land must be marked review-required"
    assert any("复核" in ln for ln in close_lines), "close must be marked review-required"


def test_openclaw_divergence_declared(skill_body):
    assert "openclaw" in skill_body
    diff_lines = [ln for ln in skill_body.splitlines() if "openclaw" in ln]
    assert any("全量授权" in ln or "差异" in ln for ln in diff_lines), (
        "the tightened land/close policy vs openclaw must be declared explicitly"
    )


def test_worker_worktree_isolation_fields(skill_body):
    assert "../repair-sweep-wt/<item-id>" in skill_body, (
        "spawn prompt template must embed the per-worker worktree path"
    )
    assert "禁止改主工作区" in skill_body


# ---------------------------------------------------------------------------
# Worktree lifecycle (plan §3.4 item 3)
# ---------------------------------------------------------------------------


def test_worktree_lifecycle_commands(skill_body):
    assert "git worktree add ../repair-sweep-wt/<item-id> -b fix/<item-id>" in skill_body
    assert "git worktree remove" in skill_body
    assert "git worktree prune" in skill_body
    # Cleanup verification: git worktree list is the closing check.
    assert "git worktree list" in skill_body
    _assert_ordered(
        skill_body,
        ["git worktree add", "git worktree remove", "git worktree prune", "git worktree list"],
    )


def test_worktree_cleanup_is_a_gate(skill_body):
    """A sweep may not be declared finished before cleanup is verified."""
    gate_lines = [ln for ln in skill_body.splitlines() if "git worktree list" in ln]
    assert any(("不得" in ln or "收尾" in ln) for ln in gate_lines)


# ---------------------------------------------------------------------------
# Challenge channel, fallback and ledger
# ---------------------------------------------------------------------------


def test_challenge_channel_and_trail(skill_body):
    assert "sessions_send" in skill_body, "workers challenge via the A2A channel"
    assert "质疑" in skill_body
    # The orchestrator's response to a challenge must be recorded in the ledger.
    assert "台账" in skill_body
    assert "留痕" in skill_body


def test_kill_fallback_and_no_steer_for_swarm(skill_body):
    assert "sessions_kill" in skill_body
    steer_lines = [ln for ln in skill_body.splitlines() if "steer" in ln.lower()]
    assert steer_lines, "the no-steer-for-swarm constraint must be spelled out"
    assert any(("不接受" in ln or "拒绝" in ln) and "重派" in ln for ln in steer_lines), (
        "steer is rejected for swarm runs: correction is kill + respawn"
    )
