"""Skill-review gate: snapshots, rollback, judge verdicts, no-op handling."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import agent.middlewares.summarization.skill_review as quality

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _Snapshot:
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.dirs = frozenset()


class _FakeJudgeModel:
    def __init__(self, payload: dict | Exception):
        self.payload = payload

    async def ainvoke(self, _messages):
        if isinstance(self.payload, Exception):
            raise self.payload
        return SimpleNamespace(content=json.dumps(self.payload))


def _install_judge(monkeypatch, payload):
    import models

    monkeypatch.setattr(models, "build_auxiliary_llm", lambda **_kwargs: _FakeJudgeModel(payload))


def _verdict(**overrides):
    payload = {
        "verdict": "pass",
        "grounded_in_run": True,
        "reusable": True,
        "trigger_quality": 8,
        "quality": 8,
        "no_op_justified": False,
        "feedback": "",
    }
    payload.update(overrides)
    return payload


def test_snapshot_and_rollback_restore_files_and_remove_new_ones(tmp_path, monkeypatch):
    auto = tmp_path / "auto"
    skill = auto / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("old", encoding="utf-8")
    monkeypatch.setattr(quality, "AUTO_SKILLS_DIR", auto)
    monkeypatch.setattr("skills.build_skills_snapshot", lambda: None)

    before = quality.snapshot_auto_skills()
    skill.write_text("changed", encoding="utf-8")
    (auto / "demo" / "references").mkdir()
    (auto / "demo" / "references" / "new.md").write_text("new", encoding="utf-8")

    after = quality.snapshot_auto_skills()
    changed = quality.changed_skill_paths(before, after)
    assert "demo/SKILL.md" in changed
    assert "demo/references/new.md" in changed

    quality.restore_auto_skills(before)

    assert skill.read_text(encoding="utf-8") == "old"
    assert not (auto / "demo" / "references" / "new.md").exists()
    assert not (auto / "demo" / "references").exists()


@pytest.mark.asyncio
async def test_no_change_without_reason_is_a_retry_without_calling_the_judge(monkeypatch):
    _install_judge(monkeypatch, _verdict())

    result = await quality.judge_skill_update({}, _Snapshot({}), _Snapshot({}), None)

    assert result.passed is False
    assert result.verdict == "retry"


@pytest.mark.asyncio
async def test_an_approved_no_op_passes(monkeypatch):
    _install_judge(monkeypatch, _verdict(no_op_justified=True))

    result = await quality.judge_skill_update(
        {"success_steps": []}, _Snapshot({}), _Snapshot({}), "no reusable signal"
    )

    assert result.passed is True
    assert result.no_op_justified is True


@pytest.mark.asyncio
async def test_an_unjustified_no_op_is_a_retry(monkeypatch):
    _install_judge(monkeypatch, _verdict(no_op_justified=False))

    result = await quality.judge_skill_update(
        {"failed_branches": ["a real pitfall"]},
        _Snapshot({}),
        _Snapshot({}),
        "no reusable signal",
    )

    assert result.passed is False


@pytest.mark.asyncio
async def test_a_grounded_reusable_change_passes(monkeypatch):
    _install_judge(monkeypatch, _verdict())
    before = _Snapshot({})
    after = _Snapshot({"demo/SKILL.md": b"new"})

    result = await quality.judge_skill_update(
        {"success_steps": [{"step": "run tests"}]}, before, after, None
    )

    assert result.passed is True
    assert result.quality == 8


@pytest.mark.asyncio
async def test_a_low_quality_change_is_retried(monkeypatch):
    _install_judge(monkeypatch, _verdict(quality=3))

    result = await quality.judge_skill_update(
        {"success_steps": [{"step": "run tests"}]},
        _Snapshot({}),
        _Snapshot({"demo/SKILL.md": b"new"}),
        None,
    )

    assert result.passed is False


@pytest.mark.asyncio
async def test_judge_unavailable_is_a_retry(monkeypatch):
    _install_judge(monkeypatch, RuntimeError("provider down"))

    result = await quality.judge_skill_update(
        {"success_steps": [{"step": "run tests"}]},
        _Snapshot({}),
        _Snapshot({"demo/SKILL.md": b"new"}),
        None,
    )

    assert result.passed is False
    assert "unavailable" in result.feedback.lower()


@pytest.mark.asyncio
async def test_a_scope_contradiction_is_a_retry(monkeypatch):
    """The judge rejects a scope that contradicts the skill body."""
    _install_judge(monkeypatch, _verdict(scope_valid=False))
    before = _Snapshot({})
    after = _Snapshot({"demo/SKILL.md": b"new"})

    result = await quality.judge_skill_update(
        {"success_steps": [{"step": "run tests"}]}, before, after, None
    )

    assert result.passed is False
    assert result.scope_valid is False
    assert "scope contradicts" in result.feedback


@pytest.mark.asyncio
async def test_a_payload_without_scope_valid_keeps_the_historical_pass(monkeypatch):
    """Older judges / other payloads omit the key: treat it as valid."""
    _install_judge(monkeypatch, _verdict())
    before = _Snapshot({})
    after = _Snapshot({"demo/SKILL.md": b"new"})

    result = await quality.judge_skill_update(
        {"success_steps": [{"step": "run tests"}]}, before, after, None
    )

    assert result.passed is True
    assert result.scope_valid is True


@pytest.mark.asyncio
async def test_an_explicitly_valid_scope_passes(monkeypatch):
    _install_judge(monkeypatch, _verdict(scope_valid=True))
    before = _Snapshot({})
    after = _Snapshot({"demo/SKILL.md": b"new"})

    result = await quality.judge_skill_update(
        {"success_steps": [{"step": "run tests"}]}, before, after, None
    )

    assert result.passed is True


def test_the_judge_prompt_asks_for_scope_validation():
    prompt = quality._JUDGE_PROMPT

    assert '"scope_valid"' in prompt
    assert "main_only" in prompt and "subagent_only" in prompt


@pytest.mark.asyncio
async def test_a_justified_no_op_passes_without_content_scores(monkeypatch):
    """M30: a one-off plan's correct no-op must stop the retry loop immediately.

    The judge returns reusable/quality/trigger = 0 and even verdict="retry"
    (the historical shape); with no files changed, ``no_op_justified`` alone is
    the decision — the review must accept it, not burn every attempt.
    """
    _install_judge(
        monkeypatch,
        _verdict(
            verdict="retry",
            grounded_in_run=False,
            reusable=False,
            trigger_quality=0,
            quality=0,
            no_op_justified=True,
            feedback="one-off task; nothing reusable",
        ),
    )

    result = await quality.judge_skill_update(
        {"success_steps": []}, _Snapshot({}), _Snapshot({}), "no reusable signal"
    )

    assert result.passed is True
    assert result.no_op_justified is True


@pytest.mark.asyncio
async def test_a_changed_skill_still_needs_the_full_quality_chain(monkeypatch):
    """The no-op fast path must not bypass quality for changed content."""
    _install_judge(
        monkeypatch,
        _verdict(verdict="pass", reusable=False, no_op_justified=True),
    )
    before = _Snapshot({})
    after = _Snapshot({"demo/SKILL.md": b"new"})

    result = await quality.judge_skill_update(
        {"success_steps": [{"step": "run tests"}]}, before, after, "claimed no-op"
    )

    assert result.passed is False


@pytest.mark.asyncio
async def test_a_no_op_with_reusable_evidence_still_retries(monkeypatch):
    """An unjustified no-op remains a retry (no fast path)."""
    _install_judge(monkeypatch, _verdict(no_op_justified=False))

    result = await quality.judge_skill_update(
        {"failed_branches": ["a real pitfall"]}, _Snapshot({}), _Snapshot({}), "no signal"
    )

    assert result.passed is False
