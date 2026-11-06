"""The plan-completion skill-quality loop: pass, retry, rollback, failure."""

from __future__ import annotations

import time
from typing import Any

import pytest
from langchain_core.messages import AIMessage

import agent.middlewares.summarization.nudges as nudges
from agent.middlewares.summarization.skill_quality import SkillJudgement, SkillTreeSnapshot

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

    monkeypatch.setattr(nudges, "state_register_mem", mem)
    monkeypatch.setattr(nudges, "state_register_db", db)
    monkeypatch.setattr(
        nudges,
        "_build_plan_context",
        lambda _sid: {"plan_name": "p", "plan_path": "x", "plan_content": "", "todos": []},
    )
    monkeypatch.setattr(
        "agent.middlewares.system_prompt.core._get_and_reload_system_prompt",
        lambda _sid: "sys",
    )
    monkeypatch.setattr(nudges, "_create_plan_extraction_agent", _create)
    monkeypatch.setattr(nudges, "judge_skill_update", _judge)
    monkeypatch.setattr(nudges, "snapshot_auto_skills", lambda: SkillTreeSnapshot())
    monkeypatch.setattr(
        nudges,
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

    await nudges._run_plan_extraction("s-noop")

    assert loop_env["restores"] == []
    assert loop_env["db"].get_state("s-noop", nudges._PLAN_SNAPSHOT_HASH_KEY) == (
        nudges._plan_snapshot_hash([])
    )


@pytest.mark.asyncio
async def test_the_prompt_carries_loaded_skills_and_user_corrections(loop_env, monkeypatch):
    monkeypatch.setattr(
        nudges,
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

    await nudges._run_plan_extraction("s-feedback")

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

    await nudges._run_plan_extraction("s-retry")

    assert len(loop_env["prompts"]) == 2
    assert "add the failed branch" in loop_env["prompts"][1]
    assert len(loop_env["restores"]) == 1
    assert loop_env["db"].get_state("s-retry", nudges._PLAN_SNAPSHOT_HASH_KEY) == (
        nudges._plan_snapshot_hash([])
    )


@pytest.mark.asyncio
async def test_exhausted_attempts_record_failure_not_success(loop_env, monkeypatch):
    monkeypatch.setitem(nudges.NUDGE, "skill_nudge_max_attempts", 1)
    loop_env["payloads"].append('{"status":"updated","skills":["demo"],"reason":"bad"}')
    loop_env["judgements"].append(_fail("still wrong"))

    await nudges._run_plan_extraction("s-fail")

    assert loop_env["db"].get_state("s-fail", nudges._PLAN_SNAPSHOT_HASH_KEY, "") == ""
    assert loop_env["db"].get_state(
        "s-fail", nudges._PLAN_SKILL_FAILED_HASH_KEY, ""
    ) == nudges._plan_snapshot_hash([])
    assert len(loop_env["restores"]) == 1


@pytest.mark.asyncio
async def test_a_failure_cooldown_skips_the_judge_loop(loop_env, monkeypatch):
    loop_env["db"].set_state(
        "s-cooldown", nudges._PLAN_SKILL_FAILED_HASH_KEY, nudges._plan_snapshot_hash([])
    )
    loop_env["db"].set_state("s-cooldown", nudges._PLAN_SKILL_FAILED_AT_KEY, time.time())
    monkeypatch.setitem(nudges.NUDGE, "skill_nudge_failure_cooldown_s", 3600)

    await nudges._run_plan_extraction("s-cooldown")

    assert loop_env["prompts"] == []
    assert loop_env["restores"] == []
