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

    # The task-level pre-check is its own unit (below); the loop tests always
    # pass it unless the test overrides this seam.
    async def _generalizable(_context):
        return reviews._GeneralizabilityVerdict(True, "reusable procedure")

    monkeypatch.setattr(reviews, "_judge_task_generalizability", _generalizable)
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


@pytest.mark.asyncio
async def test_a_passing_review_invokes_the_optional_ptc_compiler(loop_env, monkeypatch):
    seen: list[tuple] = []

    async def _compile(context, before, after, text, **kwargs):
        seen.append((context, before, after, text, kwargs))

    monkeypatch.setattr(reviews, "_compile_ptc_scripts", _compile)
    loop_env["payloads"].append('{"status":"updated","skills":["alpha"],"reason":"ok"}')
    loop_env["judgements"].append(_pass())

    await reviews._run_skill_review("s-compile")

    assert len(seen) == 1
    assert seen[0][0]["plan_name"] == "p"
    # The flow-extraction gate reads this session's effective settings.
    assert seen[0][4]["session_id"] == "s-compile"


@pytest.mark.asyncio
async def test_a_compile_failure_does_not_fail_the_skill_review(loop_env, monkeypatch):
    async def _compile(*_args, **_kwargs):
        raise RuntimeError("compiler exploded")

    monkeypatch.setattr(reviews, "_compile_ptc_scripts", _compile)
    loop_env["payloads"].append('{"status":"updated","skills":["alpha"],"reason":"ok"}')
    loop_env["judgements"].append(_pass())

    await reviews._run_skill_review("s-compile-fail")

    assert loop_env["restores"] == []
    assert loop_env["db"].get_state("s-compile-fail", reviews._SKILL_REVIEW_SNAPSHOT_HASH_KEY)


# ── task-level generalizability pre-check ────────────────────────────────────


class _JsonModel:
    """One auxiliary response (or an exception) for the pre-check."""

    def __init__(self, content: str = "", error: Exception | None = None):
        self.content = content
        self.error = error

    async def ainvoke(self, _messages):
        if self.error is not None:
            raise self.error
        return AIMessage(content=self.content)


def _patch_precheck_model(monkeypatch, model: _JsonModel) -> None:
    monkeypatch.setattr("models.build_auxiliary_llm", lambda **_kwargs: model)


@pytest.mark.asyncio
async def test_a_non_generalizable_task_skips_the_agent_and_the_judge(loop_env, monkeypatch):
    async def _no(_context):
        return reviews._GeneralizabilityVerdict(False, "one-off README typo")

    monkeypatch.setattr(reviews, "_judge_task_generalizability", _no)

    await reviews._run_skill_review("s-skip")

    assert loop_env["prompts"] == [], "no review agent may be built"
    assert loop_env["judgements"] == [], "no judge call may run"
    # The plan is consumed: the same content must not re-run the pre-check.
    assert loop_env["db"].get_state("s-skip", reviews._SKILL_REVIEW_SNAPSHOT_HASH_KEY) == (
        reviews._plan_snapshot_hash([])
    )


@pytest.mark.asyncio
async def test_a_generalizable_task_still_runs_the_review(loop_env):
    """The pre-check passing must leave the existing review + judge flow intact."""
    loop_env["payloads"].append('{"status":"no_op","reason":"nothing new"}')
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

    await reviews._run_skill_review("s-generalizable")

    assert len(loop_env["prompts"]) == 1, "the review agent must still run"
    assert loop_env["db"].get_state(
        "s-generalizable", reviews._SKILL_REVIEW_SNAPSHOT_HASH_KEY
    ) == reviews._plan_snapshot_hash([])


@pytest.mark.asyncio
async def test_the_precheck_parses_a_false_verdict(monkeypatch):
    _patch_precheck_model(
        monkeypatch,
        _JsonModel('{"should_extract": false, "reason": "one-off typo fix"}'),
    )

    verdict = await reviews._judge_task_generalizability(
        {"plan_content": "Fix the README typo", "todos": []}
    )

    assert verdict.should_extract is False
    assert "typo" in verdict.reason


@pytest.mark.asyncio
async def test_the_precheck_fails_open_on_a_malformed_answer(monkeypatch):
    _patch_precheck_model(monkeypatch, _JsonModel("not json at all"))

    verdict = await reviews._judge_task_generalizability({"plan_content": "x", "todos": []})

    assert verdict.should_extract is True
    assert "unavailable" in verdict.reason


@pytest.mark.asyncio
async def test_the_precheck_fails_open_when_the_model_raises(monkeypatch):
    _patch_precheck_model(monkeypatch, _JsonModel(error=RuntimeError("no key")))

    verdict = await reviews._judge_task_generalizability({"plan_content": "x", "todos": []})

    assert verdict.should_extract is True


@pytest.mark.asyncio
async def test_the_precheck_sees_only_the_goal_and_step_summaries(monkeypatch):
    captured: dict[str, str] = {}

    class _Capture(_JsonModel):
        async def ainvoke(self, messages):
            captured["content"] = messages[-1].content
            return await super().ainvoke(messages)

    _patch_precheck_model(monkeypatch, _Capture())

    await reviews._judge_task_generalizability(
        {
            "plan_content": "Build the project CI pipeline",
            "todos": [{"content": "write the workflow", "status": "completed"}],
            "aggregated_handoff": {"secret_marker": "SHOULD_NOT_APPEAR"},
        }
    )

    assert "Build the project CI pipeline" in captured["content"]
    assert "write the workflow" in captured["content"]
    assert "SHOULD_NOT_APPEAR" not in captured["content"]


def test_the_review_prompt_decides_scope_visibility():
    """The extraction pass must assign scope deliberately, not by accident."""
    prompt = reviews._SKILL_REVIEW_PROMPT

    assert "scope: main_only" in prompt
    assert "scope: subagent_only" in prompt
    assert "OMIT it by default" in prompt
