"""Skill-review runner (job 4): the plan-completion skill extraction pass, its judge/rollback loop and trigger gates."""

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool
from loguru import logger
from config.features import BACKGROUND_REVIEW_CONFIG
from runtime import StateKey, state_register_db, state_register_mem
from runtime.lane import LaneType, lane_slot
from .skill_review import (
    _strip_json,
    changed_skill_paths,
    judge_skill_update,
    restore_auto_skills,
    snapshot_auto_skills,
)
from ._common import _msg_to_text
from .background_scaffold import (
    StateSchema,
    _BACKGROUND_REVIEW_TASKS,
    _SKILL_REVIEW_LOCK_KEY,
    _get_iteration_budget,
)
from .handoff_tree import _build_plan_context, _compact_aggregate
from .ptc_compile import _compile_ptc_scripts


# Skill-review prompt. The plan-completion pass is skill-only: success/failure
# evidence, loaded skills and user corrections feed class-level skill updates.
# No knowledge or FACTS writes; the raw handoff tree stays as an audit file.
_SKILL_REVIEW_PROMPT = """You are a skill-review maintainer.
A plan has just completed. Update the class-level skill library from the real
success/failure evidence below. Do NOT write knowledge; the only durable output
is a skill change, or a judge-approved no-op.

## Plan context

{plan_context}

## Evidence to consume

The context contains:
- `aggregated_handoff.success_steps`: the direct procedure that worked;
- `aggregated_handoff.failed_branches`: abandoned approaches and why they failed;
- `aggregated_handoff.hard_constraints`: invariants that must always hold;
- `aggregated_handoff.locked_decisions`: decisions and their rationale;
- `feedback.loaded_skills`: skills actually loaded during this plan;
- `feedback.user_corrections`: bounded user corrections/preferences.

Map them explicitly:
- success_steps -> procedure / verification steps in the skill;
- failed_branches -> pitfalls / when_not_to_use / anti-patterns;
- hard_constraints -> guardrails / must-hold constraints;
- locked_decisions -> why / decision rationale (never a substitute for steps);
- user_corrections -> SKILL.md body or when_to_use triggers.

## Update rules

1. Prefer patching a skill in `feedback.loaded_skills`.
2. Otherwise patch the best existing class-level umbrella (use `skill_list` and
   `skill_view`).
3. Otherwise add a support file under an existing umbrella (`references/`,
   `templates/`, or `scripts/`) and add a one-line pointer in its SKILL.md.
4. Create a new class-level umbrella only when no existing skill covers the class.
5. The skill name MUST be class-level. Never use plan_key, step_id, a PR number,
   an error string, a feature codename, or a session artifact.
6. `when_to_use` is required on every created or patched skill. It must name the
   concrete situations that should trigger loading the skill.
7. Do NOT capture environment setup failures, transient errors, negative claims
   about tools, or one-off task narratives.
8. Do not execute code. You may write scripts for a skill but cannot run them.

### Module-bound carry-forward

A lesson that names a concrete file/module/test and will be hit again by the
next plan touching that module goes into a `<module>-notes` skill (for example
`auth-module-notes`). Patch an existing one instead of creating a duplicate.
Keep it to one-line symptom -> cause -> avoidance entries.

### User preferences

Put user corrections/style/workflow preferences in the governing skill's
SKILL.md body (and, when useful, its `when_to_use`), not only in memory.

## Output contract

After the tool calls, end with exactly one JSON object on its own:

{"status":"updated","skills":["skill-name"],"reason":"what changed and why"}
or
{"status":"no_op","reason":"a concrete explanation of why this plan has no reusable signal"}

A no-op is allowed only when the evidence truly contains no reusable procedure,
pitfall, or hard constraint. The quality judge will reject an unjustified no-op.
If `judge_feedback` is present in the context, address it directly and do not
repeat the rejected approach.

"""


_SKILL_REVIEW_FIRED_KEY = StateKey.SKILL_REVIEW_FIRED


#: Fingerprint of the plan content the last extraction CONSUMED — a successful
#: pass, or a plan the generalizability pre-check judged non-generalizable.
#: Either way the same plan content must not trigger another review pass.
_SKILL_REVIEW_SNAPSHOT_HASH_KEY = StateKey.SKILL_REVIEW_SNAPSHOT_HASH


#: One cheap pre-check: is this completed plan's task reusable at all?
_GENERALIZABILITY_PROMPT = """Judge whether this completed plan likely contains
reusable CLASS-LEVEL knowledge worth extracting into a skill. Answer only with a
JSON object.

## Plan goal
{goal}

## Steps
{steps}

Return: {{"should_extract": true|false, "reason": "one sentence"}}

- false: a one-off fix, a typo in a specific file, environment setup, a
  project-specific config change, or a task whose procedure only makes sense
  inside this one plan/session.
- true: a reusable procedure, pattern, workflow, or toolchain lesson that would
  help a future task of the same class.

Judge the TASK, not what a skill might be written about it. When in doubt,
answer true — the full review and its quality judge are the detailed gate."""


@dataclass(frozen=True)
class _GeneralizabilityVerdict:
    """The pre-check's answer: extract at all, and why."""

    should_extract: bool
    reason: str


_SKILL_REVIEW_FAILED_HASH_KEY = StateKey.SKILL_REVIEW_FAILED_HASH


_SKILL_REVIEW_FAILED_AT_KEY = StateKey.SKILL_REVIEW_FAILED_AT


_SKILL_REVIEW_ENABLED = BACKGROUND_REVIEW_CONFIG["skill_review_enabled"]


def _plan_snapshot_hash(todos: list[dict]) -> str:
    """Content fingerprint of the plan (id + content + status + plan_ref).

    Deliberately blind to everything else a todo row carries: the extraction is
    about the plan's own content, and a fingerprint that moved for unrelated
    bookkeeping would re-extract on every turn.
    """
    items = sorted(
        (
            {
                "id": str(todo.get("id", "")),
                "content": str(todo.get("content", "")),
                "status": str(todo.get("status", "")),
                "plan_ref": str(todo.get("plan_ref", "")),
            }
            for todo in todos
        ),
        key=lambda item: item["id"],
    )
    return hashlib.sha256(
        json.dumps(items, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _detect_todo_all_complete(session_id: str) -> bool:
    """Whether the plan is complete AND its current content is not extracted yet.

    Conditions:
    1. todos exist (non-empty)
    2. every todo is ``completed`` or ``cancelled``
    3. either nothing was extracted for this completion cycle yet, or the plan
       content CHANGED since the last successful extraction

    The once-per-cycle flag alone cannot see an in-place edit of a completed todo
    (status unchanged), which is how a changed plan used to go un-extracted; the
    snapshot fingerprint closes that hole. The flag is reset whenever the todo
    list is not (or no longer) all-complete, so the next completion extracts
    again. Reads fail open — a broken todo store must never break a turn.
    """
    try:
        from agent.tools.todolist.registry.store_sqlite import get_todos_sync

        todos = get_todos_sync(session_id)
    except Exception:
        logger.exception("todo-complete detection failed (fail-open) for {}", session_id)
        return False

    if not todos:
        return False

    all_done = all(t.get("status") in ("completed", "cancelled") for t in todos)
    if not all_done:
        state_register_db.set_state(session_id, _SKILL_REVIEW_FIRED_KEY, False)
        return False

    # A cancelled verify means acceptance was deliberately skipped (the
    # reviewer budget was exhausted, or the whole plan was abandoned). Never
    # extract/upgrade a skill from a plan that did not pass acceptance. Only the
    # LATEST verify counts: an old cancelled plan must not poison a later,
    # properly verified one in the same session.
    verifies = [t for t in todos if (t.get("metadata") or {}).get("kind") == "verify"]
    if verifies and verifies[-1].get("status") == "cancelled":
        return False

    already_fired = state_register_db.get_state(session_id, _SKILL_REVIEW_FIRED_KEY, False)
    if not already_fired:
        state_register_db.set_state(session_id, _SKILL_REVIEW_FIRED_KEY, True)
        return True

    stored = state_register_db.get_state(session_id, _SKILL_REVIEW_SNAPSHOT_HASH_KEY, "") or ""
    return _plan_snapshot_hash(todos) != stored


async def _create_skill_review_agent(system_prompt: str):
    """The skill-review fork: auxiliary model, explicit skill tool binding.

    NOT ``_create_background_review_agent``: the memory review keeps the main model (it reads
    a conversation), while this pass consumes structured plan evidence and runs
    a template-driven skill review — the same shape the step / completion judges
    already run on the auxiliary model, at a fraction of the cost and with no
    chance of crowding the main window. The system prompt is kept: the skills
    index in it decides patch-vs-create.

    The tool face is bound to exactly the skill tools the prompt drives. This
    pass must not write knowledge or facts: those outputs were retired from the
    plan-completion path, and the bound list is the barrier.
    """
    from agent.middlewares import ToolCallNormalize, ToolGuardrails
    from models import build_auxiliary_llm

    return create_agent(
        model=build_auxiliary_llm(temperature=0),
        state_schema=StateSchema,
        system_prompt=system_prompt,
        middleware=[
            ToolCallNormalize(),
            ToolGuardrails(),
            _get_iteration_budget(),
        ],
        tools=_skill_review_tool_face(),
    )


#: The skill tools the skill-review pass drives. Knowledge and memory are
#: deliberately absent: this pass only updates the skill library.
_SKILL_REVIEW_TOOL_NAMES = ("skill_list", "skill_view", "skill_manage")


def _skill_review_tool_face() -> list[BaseTool]:
    """The extraction fork's bound tools, in the main face's order."""
    from agent import get_agent_tools

    return [
        tool for tool in get_agent_tools() if getattr(tool, "name", "") in _SKILL_REVIEW_TOOL_NAMES
    ]


async def _write_handoff_tree_safely(session_id: str, context: dict[str, Any]) -> None:
    """Persist the code-merged handoff tree BEFORE the extraction LLM runs.

    An interrupted synthesis must not lose the raw merged data: the file is
    written first (atomic replace). The review's skill update is a second,
    lossy layer on top and the audit file remains available if it is rejected.
    Fail-open.
    """
    aggregated = context.get("aggregated_handoff") or {}
    if not aggregated.get("handoff_forest"):
        return
    try:
        from agent.tools.todolist.knowledge.identity import resolve_plan_identity
        from agent.tools.todolist.knowledge.knowledge_store import KnowledgeStore

        identity = resolve_plan_identity(session_id, str(context.get("plan_name") or ""))
        if identity is None:
            return
        await KnowledgeStore.write_handoff_tree(identity, aggregated)
    except Exception:
        logger.exception(
            "skill review: failed to write handoff-tree.json for {} (fail-open)",
            session_id,
        )


def _last_ai_text(res: dict[str, Any]) -> str:
    """Return the final assistant text from a skill-review invocation."""
    messages = res.get("messages") if isinstance(res, dict) else None
    for message in reversed(messages or []):
        if isinstance(message, AIMessage) or getattr(message, "type", "") == "ai":
            return _msg_to_text(message)
    return ""


def _extract_no_op_reason(text: str) -> str | None:
    """Read the skill review's final ``{"status":"no_op",...}`` JSON block."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or str(payload.get("status") or "").lower() != "no_op":
        return None
    return str(payload.get("reason") or "").strip() or "no-op without a reason"


def _skill_review_failure_active(session_id: str, snapshot_hash: str) -> bool:
    """Whether this exact plan snapshot is in a skill-review failure cooldown."""
    stored_hash = str(
        state_register_db.get_state(session_id, _SKILL_REVIEW_FAILED_HASH_KEY, "") or ""
    )
    if stored_hash != snapshot_hash:
        return False
    try:
        failed_at = float(state_register_db.get_state(session_id, _SKILL_REVIEW_FAILED_AT_KEY, 0))
    except (TypeError, ValueError):
        failed_at = 0.0
    cooldown = max(0, int(BACKGROUND_REVIEW_CONFIG["skill_review_failure_cooldown_s"]))
    return failed_at > 0 and (time.time() - failed_at) < cooldown


def _mark_skill_review_failed(session_id: str, snapshot_hash: str) -> None:
    state_register_db.set_state(session_id, _SKILL_REVIEW_FAILED_HASH_KEY, snapshot_hash)
    state_register_db.set_state(session_id, _SKILL_REVIEW_FAILED_AT_KEY, time.time())


def _clear_skill_review_failed(session_id: str) -> None:
    state_register_db.set_state(session_id, _SKILL_REVIEW_FAILED_HASH_KEY, "")
    state_register_db.set_state(session_id, _SKILL_REVIEW_FAILED_AT_KEY, 0)


def _generalizability_input(context: dict[str, Any]) -> tuple[str, str]:
    """The bounded view the pre-check receives: the goal + step summaries.

    Deliberately NOT the review's evidence (handoffs, user corrections, changed
    files): the question is whether the TASK class is reusable, and a shorter
    prompt keeps the call cheap.
    """
    goal = " ".join(str(context.get("plan_content") or "").split())[:1200]
    steps = [
        {
            "task": " ".join(str(todo.get("content") or "").split())[:200],
            "status": str(todo.get("status") or ""),
        }
        for todo in (context.get("todos") or [])[:12]
    ]
    return (
        goal or "(no plan file; the completed todo list below is the plan)",
        json.dumps(steps, ensure_ascii=False, indent=2),
    )


def _parse_generalizability(text: str) -> _GeneralizabilityVerdict:
    """Parse the pre-check's JSON answer (raises on a malformed answer)."""
    data = _strip_json(text)
    raw = data.get("should_extract")
    should_extract = raw is True or str(raw).strip().lower() == "true"
    reason = " ".join(str(data.get("reason") or "").split())[:300]
    return _GeneralizabilityVerdict(should_extract, reason or "(no reason given)")


async def _judge_task_generalizability(context: dict[str, Any]) -> _GeneralizabilityVerdict:
    """One short auxiliary call: is this plan worth the full review loop?

    The full review costs a multi-turn tool-using agent plus the quality judge;
    a one-off task ("fix this typo") should not pay that. This pre-check judges
    the TASK class (the gap the judge's ``reusable`` field does not cover), and
    an accepted plan still runs the existing review + judge unchanged.

    Fail-open to True: an unavailable or malformed pre-check must never stop the
    extraction path. The call runs inside the background-review lane, like the
    review itself, so it cannot bypass the scheduling cap.
    """
    goal, steps = _generalizability_input(context)
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from models import build_auxiliary_llm

        async with lane_slot(LaneType.BACKGROUND_REVIEW):
            model = build_auxiliary_llm(temperature=0)
            response = await model.ainvoke(
                [
                    SystemMessage(content="Return only the requested JSON object."),
                    HumanMessage(content=_GENERALIZABILITY_PROMPT.format(goal=goal, steps=steps)),
                ]
            )
        raw = response.content if hasattr(response, "content") else str(response)
        return _parse_generalizability(str(raw))
    except Exception as exc:  # noqa: BLE001 - a broken pre-check must not block extraction
        logger.warning(
            "skill review: generalizability pre-check unavailable ({}: {}); continuing",
            type(exc).__name__,
            exc,
        )
        return _GeneralizabilityVerdict(True, f"pre-check unavailable ({type(exc).__name__})")


async def _run_skill_review(session_id: str) -> None:
    """Run the skill review with judge/rollback/retry.

    The plan's raw handoff tree is written first for audit. The review then
    receives a bounded view of the evidence, loaded skills and user corrections;
    its only tools are the skill trio (``skill_list`` / ``skill_view`` /
    ``skill_manage``). No knowledge or facts are written.

    Each attempt snapshots ``skills/auto`` first. A judge-approved change (or a
    justified no-op) advances the success fingerprint; a rejected attempt is
    restored and retried with the judge's feedback. Exhaustion records a failure
    marker and leaves the plan eligible for a later retry after the cooldown.
    """
    state_register_mem.set_state(session_id, _SKILL_REVIEW_LOCK_KEY, True)
    try:
        from agent.middlewares.system_prompt.core import _get_and_reload_system_prompt
        from agent.tools.pub_base.skill_provenance import (
            BACKGROUND_REVIEW as BACKGROUND_REVIEW_ORIGIN,
            reset_current_write_origin,
            set_current_write_origin,
        )

        context = _build_plan_context(session_id)
        if not context:
            logger.debug("skill review: no plan context for session {}", session_id)
            return

        snapshot_hash = _plan_snapshot_hash(list(context.get("todos") or []))
        if _skill_review_failure_active(session_id, snapshot_hash):
            logger.debug("skill review: skill-review failure cooldown active for {}", session_id)
            return

        # Task-level pre-check BEFORE building the review agent: a one-off plan
        # should not pay a multi-turn agent + judge. Storing the snapshot as
        # consumed keeps the same plan content from re-running the pre-check on
        # every turn (the existing fingerprint gate reads this key).
        verdict = await _judge_task_generalizability(context)
        if not verdict.should_extract:
            logger.info(
                "skill review: skipped for {} — task judged non-generalizable: {}",
                session_id,
                verdict.reason,
            )
            state_register_db.set_state(session_id, _SKILL_REVIEW_SNAPSHOT_HASH_KEY, snapshot_hash)
            return

        # Raw merged tree first (code), then the compact view for the LLM.
        await _write_handoff_tree_safely(session_id, context)
        render_context = dict(context)
        render_context.pop("plan_snapshot", None)
        render_context["aggregated_handoff"] = _compact_aggregate(
            context.get("aggregated_handoff") or {}
        )
        system_prompt = _get_and_reload_system_prompt(session_id)

        attempts = max(1, int(BACKGROUND_REVIEW_CONFIG["skill_review_max_attempts"]))
        before = snapshot_auto_skills()
        feedback = ""
        for attempt in range(1, attempts + 1):
            prompt_context = dict(render_context)
            if feedback:
                prompt_context["judge_feedback"] = feedback
            prompt = _SKILL_REVIEW_PROMPT.replace(
                "{plan_context}", json.dumps(prompt_context, ensure_ascii=False, indent=2)
            )
            async with lane_slot(LaneType.BACKGROUND_REVIEW):
                agent = await _create_skill_review_agent(system_prompt)
                token = set_current_write_origin(BACKGROUND_REVIEW_ORIGIN)
                try:
                    res = await agent.ainvoke(
                        input={
                            "session_id": session_id,
                            "messages": [HumanMessage(content=prompt)],
                        }
                    )
                finally:
                    reset_current_write_origin(token)

            text = _last_ai_text(res)
            logger.debug("skill review attempt {}/{} result: {}", attempt, attempts, text[:2000])
            after = snapshot_auto_skills()
            judgement = await judge_skill_update(
                render_context,
                before,
                after,
                _extract_no_op_reason(text),
            )
            logger.info(
                "skill-review attempt {}/{}: pass={} verdict={} grounded={} "
                "reusable={} trigger={} quality={} changed={} reason={}",
                attempt,
                attempts,
                judgement.passed,
                judgement.verdict,
                judgement.grounded_in_run,
                judgement.reusable,
                judgement.trigger_quality,
                judgement.quality,
                len(changed_skill_paths(before, after)),
                judgement.feedback,
            )
            if judgement.passed:
                _clear_skill_review_failed(session_id)
                state_register_db.set_state(
                    session_id, _SKILL_REVIEW_SNAPSHOT_HASH_KEY, snapshot_hash
                )
                # PTC compilation is a separate, best-effort phase: it must
                # never turn an accepted skill update into a failed review.
                try:
                    await _compile_ptc_scripts(context, before, after, text)
                except Exception:
                    logger.exception("PTC compile failed (fail-open) for session {}", session_id)
                return

            restore_auto_skills(before)
            feedback = judgement.feedback

        logger.warning(
            "skill-review exhausted {} attempt(s) for {}; rolled back skills/auto",
            attempts,
            session_id,
        )
        _mark_skill_review_failed(session_id, snapshot_hash)
    except Exception:
        logger.exception("skill review failed (fail-open) for {}", session_id)
    finally:
        state_register_mem.set_state(session_id, _SKILL_REVIEW_LOCK_KEY, False)


async def maybe_trigger_skill_review(session_id: str) -> None:
    """Fork the extraction pass when the plan just finished (fail-open).

    Called by the plan-completion hook (``PlanContinuationEnforcer`` at the end
    of a turn whose plan has no incomplete rows). Independent of composition and
    of the window: a session whose plan completes after two turns extracts just
    like one that took fifty, and no conversation history rides along.

    Every gate is checked BEFORE the completion detector (which consumes the
    once-per-cycle flag) so a bail-out never marks a plan as extracted.
    """
    if not session_id or not _SKILL_REVIEW_ENABLED:
        return
    if state_register_mem.get_state(session_id, _SKILL_REVIEW_LOCK_KEY, False):
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("skill review: no running event loop, skipping for {}", session_id)
        return
    if not _detect_todo_all_complete(session_id):
        return

    coro = _run_skill_review(session_id)
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:  # pragma: no cover - the loop vanished after the check
        coro.close()
        return
    _BACKGROUND_REVIEW_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_REVIEW_TASKS.discard)
    logger.debug("skill review scheduled for session {}", session_id)
