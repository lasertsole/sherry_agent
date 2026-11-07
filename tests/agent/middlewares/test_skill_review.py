"""The plan-completion skill-review loop: pass, retry, rollback, failure."""

from __future__ import annotations

import time
from typing import Any

import pytest
from langchain_core.messages import AIMessage

import agent.middlewares.summarization.background_reviews as reviews
from agent.middlewares.summarization.skill_review import SkillJudgement, SkillTreeSnapshot

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _State:
    def __init__(self):
        self.data: dict[tuple[str, str], Any] = {}

    def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        return self.data.get((session_id, str(key)), default)

    def set_state(self, session_id: str, key: str, value: Any) -> None:
        self.data[(session_id, str(key))] = value


def _pass(feedback: str = "") -> SkillJudgement:
    return SkillJudgement(
        passed=True,
        verdict="pass",
        grounded_in_run=True,
        reusable=True,
        trigger_quality=8,
        quality=8,
        no_op_justified=False,
        feedback=feedback,
    )


def _fail(feedback: str = "make it more grounded") -> SkillJudgement:
    return SkillJudgement(
        passed=False,
        verdict="retry",
        grounded_in_run=False,
        reusable=False,
        trigger_quality=0,
        quality=0,
        no_op_justified=False,
        feedback=feedback,
    )


@pytest.fixture()
def loop_env(monkeypatch):
    mem = _State()
    db = _State()
    prompts: list[str] = []
    restores: list[SkillTreeSnapshot] = []
    judgements: list[SkillJudgement] = []
    payloads: list[str] = []

    class _Agent:
        def __init__(self, payload: str):
            self.payload = payload

        async def ainvoke(self, input, **_kwargs):
            prompts.append(input["messages"][-1].content)
            return {"messages": [AIMessage(content=self.payload)]}

    async def _create(_system_prompt: str):
        index = min(len(payloads) - 1, len(prompts))
        return _Agent(payloads[index])

    async def _judge(*_args, **_kwargs):
        return judgements.pop(0)

    monkeypatch.setattr(reviews, "state_register_mem", mem)
    monkeypatch.setattr(reviews, "state_register_db", db)
    monkeypatch.setattr(
        reviews,
        "_build_plan_context",
        lambda _sid: {"plan_name": "p", "plan_path": "x", "plan_content": "", "todos": []},
    )
    monkeypatch.setattr(
        "agent.middlewares.system_prompt.core._get_and_reload_system_prompt",
        lambda _sid: "sys",
    )
    monkeypatch.setattr(reviews, "_create_skill_review_agent", _create)
    monkeypatch.setattr(reviews, "judge_skill_update", _judge)
    monkeypatch.setattr(reviews, "snapshot_auto_skills", lambda: SkillTreeSnapshot())
    monkeypatch.setattr(
        reviews,
        "restore_auto_skills",
        lambda snapshot: restores.append(snapshot),
    )
    return {
        "mem": mem,
        "db": db,
        "prompts": prompts,
        "restores": restores,
        "judgements": judgements,
        "payloads": payloads,
    }


@pytest.mark.asyncio
async def test_a_judge_approved_no_op_marks_the_plan_extracted(loop_env):
    loop_env["payloads"].append('{"status":"no_op","reason":"config-only change"}')
    loop_env["judgements"].append(
        SkillJudgement(
            passed=True,
            verdict="pass",
            grounded_in_run=True,
            reusable=True,
            trigger_quality=8,
            quality=8,
            no_op_justified=True,
            feedback="",
        )
    )

    await reviews._run_skill_review("s-noop")

    assert loop_env["restores"] == []
    assert loop_env["db"].get_state("s-noop", reviews._SKILL_REVIEW_SNAPSHOT_HASH_KEY) == (
        reviews._plan_snapshot_hash([])
    )


@pytest.mark.asyncio
async def test_the_prompt_carries_loaded_skills_and_user_corrections(loop_env, monkeypatch):
    monkeypatch.setattr(
        reviews,
        "_build_plan_context",
        lambda _sid: {
            "plan_name": "p",
            "plan_path": "x",
            "plan_content": "",
            "todos": [],
            "feedback": {
                "loaded_skills": [
                    {"name": "alpha", "description": "Alpha skill", "when_to_use": "alpha work"}
                ],
                "user_corrections": ["use passlib, not bcrypt"],
            },
        },
    )
    loop_env["payloads"].append('{"status":"no_op","reason":"nothing reusable"}')
    loop_env["judgements"].append(
        SkillJudgement(
            passed=True,
            verdict="pass",
            grounded_in_run=True,
            reusable=True,
            trigger_quality=8,
            quality=8,
            no_op_justified=True,
            feedback="",
        )
    )

    await reviews._run_skill_review("s-feedback")

    prompt = loop_env["prompts"][0]
    assert "alpha" in prompt and "Alpha skill" in prompt
    assert "use passlib, not bcrypt" in prompt


@pytest.mark.asyncio
async def test_a_rejected_attempt_is_restored_and_retried_with_feedback(loop_env):
    loop_env["payloads"].extend(
        [
            '{"status":"updated","skills":["demo"],"reason":"first"}',
            '{"status":"updated","skills":["demo"],"reason":"second"}',
        ]
    )
    loop_env["judgements"].extend([_fail("add the failed branch"), _pass()])

    await reviews._run_skill_review("s-retry")

    assert len(loop_env["prompts"]) == 2
    assert "add the failed branch" in loop_env["prompts"][1]
    assert len(loop_env["restores"]) == 1
    assert loop_env["db"].get_state("s-retry", reviews._SKILL_REVIEW_SNAPSHOT_HASH_KEY) == (
        reviews._plan_snapshot_hash([])
    )


@pytest.mark.asyncio
async def test_exhausted_attempts_record_failure_not_success(loop_env, monkeypatch):
    monkeypatch.setitem(reviews.BACKGROUND_REVIEW_CONFIG, "skill_review_max_attempts", 1)
    loop_env["payloads"].append('{"status":"updated","skills":["demo"],"reason":"bad"}')
    loop_env["judgements"].append(_fail("still wrong"))

    await reviews._run_skill_review("s-fail")

    assert loop_env["db"].get_state("s-fail", reviews._SKILL_REVIEW_SNAPSHOT_HASH_KEY, "") == ""
    assert loop_env["db"].get_state(
        "s-fail", reviews._SKILL_REVIEW_FAILED_HASH_KEY, ""
    ) == reviews._plan_snapshot_hash([])
    assert len(loop_env["restores"]) == 1


@pytest.mark.asyncio
async def test_a_failure_cooldown_skips_the_judge_loop(loop_env, monkeypatch):
    loop_env["db"].set_state(
        "s-cooldown", reviews._SKILL_REVIEW_FAILED_HASH_KEY, reviews._plan_snapshot_hash([])
    )
    loop_env["db"].set_state("s-cooldown", reviews._SKILL_REVIEW_FAILED_AT_KEY, time.time())
    monkeypatch.setitem(reviews.BACKGROUND_REVIEW_CONFIG, "skill_review_failure_cooldown_s", 3600)

    await reviews._run_skill_review("s-cooldown")

    assert loop_env["prompts"] == []
    assert loop_env["restores"] == []
