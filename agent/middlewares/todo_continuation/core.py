"""Plan-continuation enforcer — a turn-end ``after_agent`` middleware.

The gate covers the whole PLAN: the session's todo list AND its non-terminal
TaskFlow flows. A turn that ends with work left in either surface gets a
continuation directive that names what remains and the tool call that closes it.
(The module/class keep their ``todo_continuation`` names for compatibility; the
todo list is one of the two plan surfaces, not the gate's whole scope.) Delivery reuses the existing
fire-and-forget auto-turn infrastructure, resolved through ``runtime.hooks``
(the agent layer must not reach up into the server layer): the prompt is
handed to the registered trigger as a ``HumanMessage`` and injected on the
next idle turn — the middleware itself never mutates state. When no trigger
is registered (evals, unit tests, any process that never assembled a server)
the injection is a no-op and the session stays retryable.

Timing note: this hook runs INSIDE the turn whose end it observes, so the
session still reads busy (`ws_task`/`answering`) at that instant. The server's
auto-turn trigger therefore DEFERS past those two self-busy signals (bounded by
``TODOLIST_INFRA["continuation_self_idle_wait_s"]``) instead of refusing — a
session still busy at the cap is a real user turn and the directive is dropped,
with the next turn end deciding again.

The gate is BOUNDED by a consecutive-continuation ceiling. Stagnation catches a
plan that stops moving, but a model that edits the plan just enough to keep the
snapshot changing (one status flip, one added step) never trips it — and its
inject→auto-turn→inject loop has no other stop. ``mark_injected`` (the one point
that runs only after the trigger CONFIRMED delivery) also counts the turn; at
``TODOLIST_INFRA["max_continuation_turns"]`` the gate delivers ONE final
stop-and-summarize directive and then stays silent until a real user message —
or a subagent-completion turn, which is a new event rather than a continuation —
resets the budget.

Tailored from omo ``todo-continuation-enforcer``:

- idle detection → ``auto_turn`` + ``detect_state`` (already present);
- 2.5s countdown → ``auto_turn`` fire-and-forget (already present);
- stagnation/backoff/abort/recovery → ``agent/tools/todolist/stagnation_tracker``;
- user-takeover / assistant-activity cancellation → ``auto_turn`` internals.

Hook contract (installed ``langchain 1.3.9``): ``aafter_agent(state, runtime)``
returns ``None``; it is NOT a state update. Registered FIRST in the middleware
list because after_agent hooks run in REVERSE list order — first registered runs
LAST (truly at the end of the turn).

Hard rules honored: blank/missing session → no-op; abort-class turn error → no
continuation; any internal failure is swallowed (log + return None) so the
enforcer can never break a turn.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from loguru import logger

from agent.tools.taskflow.progress_push import steps_of
from agent.tools.taskflow.tools._shared import steps_summary
from config.features import TODOLIST_INFRA
from runtime import hooks

__all__ = ["PlanContinuationEnforcer", "TodoContinuationEnforcer"]

# Continuation directive (behavior contract of this module).
# Origin tag of every injected directive from this middleware.
_ORIGIN = "todo_continuation"

#: Turns without a ``todowrite`` after which the continuation directive carries
#: the tracking nudge. ``todowrite`` is the model's only way to keep the TODO
#: list honest, and an agent advancing TaskFlow clears the stagnation streak
#: (the plan snapshot changes) while the list rots — that window is what this
#: catches. The nudge is APPENDED to the continuation directive, never a
#: directive of its own.
_TODO_WRITE_REMINDER_TURNS = TODOLIST_INFRA["todo_write_nudge_turns"]

_TODO_WRITE_REMINDER = """

[REMINDER: TODO TRACKING]
You have not used the todowrite tool in {n} turns. If task statuses have
changed, use todowrite to keep the TODO list in sync — the list above may be
stale. This is a gentle reminder; ignore it if the list is still accurate.
"""

_CONTINUATION_PROMPT = """[SYSTEM DIRECTIVE: TODO CONTINUATION]

Incomplete tasks remain in your todo list. Continue working on the next pending task.

- Proceed without asking for permission
- Mark each task complete when finished
- Do not stop until all tasks are done
- If you believe all work is complete, the system is questioning your completion claim.
  Critically re-examine each todo item, verify the work was actually done, and update accordingly.
- A TaskFlow flow counts as finished only after taskflow_flow(action='finish') accepted it (its
  gates verify DAG completeness and the evidence) — a run of green steps with the
  flow still open is NOT completion.

{todo_status}
{flow_status}"""

# Blocked-only directive: every remaining TaskFlow step is blocked, so
# "continue working on the next pending task" would point at work the model
# cannot advance. The decision it CAN make is named instead.
_BLOCKED_ONLY_PROMPT = """[SYSTEM DIRECTIVE: TASKFLOW BLOCKED]

All remaining work is blocked — continuing to work cannot advance it. A blocked
step's result was never proven (a failed judge verdict, a budget-exhausted child,
or an upstream that settled without a result), so do NOT mark it done.

Review the block reasons in the board and pick one action per blocked step:
- replan around the blocker: taskflow_flow(action='replan', flow_id=..., reason=...)
- cancel the blocked branch: taskflow_step(action='update_steps', ...) with that step cancelled,
  or cancel the whole flow: taskflow_flow(action='cancel', flow_id=..., reason=...)
- resolve the step by injecting the missing result: taskflow_step(action='resume', flow_id=...)

{todo_status}
{flow_status}"""

# Recovery directive (behavior contract of this module).
_RECOVERY_PROMPT = """[SYSTEM DIRECTIVE: RECOVERY MODE]

Stagnation detected — the todo list has not changed across multiple continuation attempts.

Recovery actions:
1. Run todoread to see the exact current state
2. For each incomplete task, ask: "Is this actually done but unmarked, or genuinely incomplete?"
3. If done: mark completed via todowrite (verify first — Sisyphus contract)
4. If incomplete: re-plan the task — break it down differently or delegate to a subagent
5. If blocked: mark cancelled and note the blocker

Do NOT repeat the same actions that led to stagnation.

{todo_status}
{flow_status}"""

# Completion directive: the last thing the gate ever sends before it latches.
# The model is told to stop and summarize — the plan stays open, and the user
# (or a later turn) decides whether to resume.
_STOP_PROMPT = """[SYSTEM DIRECTIVE: CONTINUATION BUDGET EXHAUSTED]

The automatic continuation budget is exhausted ({used}/{cap} continuation turns).
The plan is still open, but you must stop working on it now.

- Stop calling tools.
- Give the user a final summary: what you accomplished, what remains, and the
  state the plan is left in.
- Do not start new work. The user can continue by sending a new message.

{todo_status}
{flow_status}"""

# Final-verification reminder: the plan is complete, but the agent may be
# overstating the work. This is a PROMPT, not a gate — the agent re-checks and
# either keeps fixing or confirms; nothing here inspects evidence or blocks.
_VERIFICATION_REMINDER = """[SYSTEM DIRECTIVE: FINAL VERIFICATION CHECK]

Every plan item is complete and no TaskFlow flow is open. Before you report
success, assume the work is broken until you have proved otherwise — the common
failure is marking work done when it is not.

Verify it yourself now:
- Read what you changed (`git diff` / the edited files): confirm it matches the
  request, with no stubs, TODOs, placeholders or unrequested scope creep.
- Actually run the checks (tests / typecheck / lint) through the terminal tool.
  "It should pass" is not evidence; a real command output is.
- For user-facing changes, launch it and exercise it — UI, CLI or API — and read
  the output.
- "Probably works" = NOT verified. Keep fixing until you are certain.

If something is wrong, fix it and continue (do not mark the plan complete).
Only when you can point at the proof may you finish and summarize.
"""

#: Verify-todo guidance appended to a continuation when the system [VERIFY]
#: todo is the only remaining item. The report schema is part of the spawn
#: brief the main agent writes; the tool layer never parses the report.
_VERIFY_DIRECTIVE = """
[VERIFY TODO]
The only remaining item is the system [VERIFY] todo. Acceptance is independent:
- If no reviewer is linked yet: read every work todo's acceptance criteria, then
  spawn sessions_spawn(functional_role="reviewer") with a brief that lists each
  todo_id + its criteria + the exact command(s). Require this JSON report:
  {"verified": bool, "results": [{"todo_id": N, "passed": bool, "evidence": "...", "suggested_fix": "..."}]}
  Patch the returned child_session_key into this todo's subagent_id.
- If a reviewer finished: read its report (the completion notice / sessions_messages).
  For every passed=false todo_id, patch that todo back to pending and put the
  evidence in its notes. Do not rewrite the whole list; passed todos stay completed.
  If the report is unreadable, spawn a new reviewer (the gate allows a bounded number
  of re-spawns before the exhausted escape).
- Only a {"verified": true} report may complete this todo; the transition barrier
  requires a linked, finished reviewer.
"""

#: Injected once when the reviewer link was re-spawned past its budget. The main
#: agent may cancel the verify todo and run the acceptance checks itself — a
#: degraded close instead of a permanently stuck plan.
_VERIFY_EXHAUSTED_PROMPT = """[SYSTEM DIRECTIVE: VERIFY BUDGET EXHAUSTED]

The verify todo's reviewer was re-spawned past its budget without a readable
report. Do not spawn another reviewer.

- Run the acceptance checks yourself now (terminal/python_repl) and fix what fails.
- Then patch the verify todo to cancelled (you may also cancel it first and run the
  checks — the transition barrier allows this escape while the flag is set).
- The plan will be recorded as NOT verified; do not claim acceptance passed.

{todo_status}"""

#: Process-local reminder throttle: session_id -> fingerprint of the completed
#: plan that already received the reminder. A changed plan gets a new reminder.
_reminded_plans: dict[str, str] = {}

#: Statuses that still need someone to act — the union of the todo vocabulary
#: (``pending``/``in_progress``) and the TaskFlow step vocabulary
#: (``ready``/``blocked``/``dispatched``); the gate reads the plan, which uses both.
_STEP_INCOMPLETE_STATUSES = ("ready", "blocked", "dispatched")
_INCOMPLETE_STATUSES = ("pending", "in_progress", *_STEP_INCOMPLETE_STATUSES)


# ── the plan: todos + non-terminal flows ─────────────────────────────────────

#: Steps per flow rendered into the directive before it is clipped.
_MAX_STEPS_PER_FLOW = 20
#: Task text width inside the directive.
_TASK_WIDTH = 120


def _active_flows(session_id: str) -> list[dict]:
    """The session's non-terminal flows (running/waiting); [] on any failure.

    Imported lazily and defensively: the gate must degrade to the todo half
    rather than break a turn when the taskflow package is unavailable.
    """
    try:
        from agent.tools.taskflow.registry.store_sqlite import get_active_flows_sync

        return get_active_flows_sync(session_id)
    except Exception:
        logger.debug("plan continuation: taskflow read failed; continuing with todos only")
        return []


def _flow_rows(flows: list[dict]) -> list[dict]:
    """Tracker-shaped rows for the plan snapshot: one per step, plus closure rows.

    A flow whose steps are all settled but whose status is still running/waiting
    contributes a ``<flow>:finish`` row — the plan is not closed until the flow is
    (``taskflow_flow(action='finish')`` accepted it, or it was cancelled), and that state is
    exactly the silent-open-flow case the gate exists for.
    """
    rows: list[dict] = []
    for flow in flows:
        flow_id = str(flow.get("flow_id") or "")
        steps = steps_of(flow)
        for step in steps:
            rows.append(
                {
                    "content": f"{flow_id}/{step.get('step_id')}",
                    "status": str(step.get("status") or "ready"),
                }
            )
        if steps and all(_step_settled(step) for step in steps):
            rows.append({"content": f"{flow_id}:finish", "status": "in_progress"})
    return rows


def _step_settled(step: dict) -> bool:
    """True when a step needs no further action (done or a decision was recorded)."""
    return str(step.get("status") or "ready") not in _STEP_INCOMPLETE_STATUSES


def _blocked_only(todos: list[dict], flows: list[dict]) -> bool:
    """True when the only remaining work is blocked TaskFlow steps.

    A pending/in-progress todo is work the model CAN advance, a ready or
    dispatched step is pending action or in flight, and a flow whose steps are
    all settled but still open needs a finish call — only an all-blocked step
    set with none of those makes "continue working" the wrong instruction.
    """
    if any(t.get("status") in ("pending", "in_progress") for t in todos):
        return False
    flow_statuses: list[str] = []
    for flow in flows:
        steps = steps_of(flow)
        if steps and all(_step_settled(step) for step in steps):
            return False  # the remaining action is taskflow_flow(action='finish')
        flow_statuses.extend(
            status
            for step in steps
            if (status := str(step.get("status") or "ready")) in _STEP_INCOMPLETE_STATUSES
        )
    return bool(flow_statuses) and all(status == "blocked" for status in flow_statuses)


def _build_flow_block(flows: list[dict]) -> str:
    """Render the TaskFlow half of the directive (empty when nothing is open)."""
    if not flows:
        return ""
    lines = ["TaskFlow board (open flows):"]
    for flow in flows:
        flow_id = str(flow.get("flow_id") or "")
        steps = steps_of(flow)
        state = flow.get("state") or {}
        description = str(state.get("description") or "")[:80]
        counts = steps_summary(steps)
        lines.append(
            f"- {flow_id} [{flow.get('status')}] {description} — "
            f"{counts['done']}/{len(steps)} steps done"
        )
        clipped = steps[:_MAX_STEPS_PER_FLOW]
        for step in clipped:
            step_id = str(step.get("step_id") or "")
            status = str(step.get("status") or "ready")
            task = str(step.get("task") or "")[:_TASK_WIDTH]
            lines.append(f"  step {step_id} ({status}): {task}")
        extra = len(steps) - len(clipped)
        if extra > 0:
            lines.append(f"  … +{extra} more steps")
        if steps and all(_step_settled(step) for step in steps):
            lines.append(
                "  every step is settled but the flow is still open: close it with "
                "taskflow_flow(action='finish') (or taskflow_flow(action='cancel') if the work is abandoned)"
            )
    lines.append(
        "Board actions: dispatched steps need taskflow_step(action='wait_all') then "
        "taskflow_step(action='resume') to inject their results; ready steps are "
        "auto-dispatched (retry a failed spawn with taskflow_step(action='dispatch')); "
        "blocked steps need a decision — replan, cancel that branch, or inject the "
        "missing result."
    )
    return "\n".join(lines)


# The turn error is recorded by the runtime under one of these optional state
# keys; none exist in the base AgentState schema, so absence is the normal case.
_ERROR_STATE_KEYS = ("error", "turn_error", "exception")


def _build_status_block(todos: list[dict]) -> str:
    """Render the design-doc status block: counts + per-todo icons + notes."""
    done = sum(1 for t in todos if t["status"] in ("completed", "cancelled"))
    total = len(todos)
    remaining = [t for t in todos if t["status"] in _INCOMPLETE_STATUSES]
    lines = [f"[Status: {done}/{total} completed, {len(remaining)} remaining]"]
    lines.append("Remaining tasks:")
    for todo in remaining:
        icon = {"pending": "○", "in_progress": "◐"}.get(todo["status"], "○")
        step_ref = ""
        if todo.get("flow_id") and todo.get("step_id"):
            step_ref = f" (flow {todo['flow_id']} / {todo['step_id']})"
        lines.append(f"- [{icon}] {todo['content']} ({todo['priority']}){step_ref}")
        notes = str(todo.get("notes") or "").strip()
        if notes:
            lines.append(f"  notes: {notes[:400]}")
    return "\n".join(lines)


def _build_verify_block(verify: dict, state: str) -> str:
    """The verify-specific continuation text (or "" when not the only item)."""
    if state == "running":
        return ""
    lines = [_VERIFY_DIRECTIVE.rstrip()]
    if state == "none":
        lines.append("No reviewer is linked yet: spawn one and patch its child_session_key.")
    elif state == "finished":
        lines.append("A reviewer finished: read its report and act on it.")
    return "\n".join(lines)


def _plan_fingerprint(todos: list[dict], flow_rows: list[dict]) -> str:
    """Stable hash of the completed plan (todo rows + flow closure rows)."""
    parts = [f"{row.get('content')}|{row.get('status')}" for row in [*todos, *flow_rows]]
    return "\x1f".join(sorted(parts))


def _turn_error(state: Any) -> BaseException | None:
    """Extract a turn-end exception from optional state keys, if recorded."""
    if not isinstance(state, dict):
        return None
    for key in _ERROR_STATE_KEYS:
        value = state.get(key)
        if isinstance(value, BaseException):
            return value
    return None


_hook_misses_logged: set[str] = set()


def _resolve_hook(name: str) -> Callable[..., Any] | None:
    """Resolve a runtime hook, logging the first miss per name (once)."""
    fn = hooks.resolve(name)
    if fn is None and name not in _hook_misses_logged:
        _hook_misses_logged.add(name)
        logger.debug(
            "plan continuation: runtime hook {!r} is not registered; "
            "continuation delivery degrades to a no-op",
            name,
        )
    return fn


def _turn_used_todowrite(state: dict[str, Any]) -> bool:
    """True when the turn's messages carry a ``todowrite`` tool call.

    Read from the messages rather than hooked into the tool itself: the tool
    layer must not know this middleware exists, and the turn's transcript is
    right here. A tool call (AIMessage.tool_calls) is enough — a denied or
    failed call still means the model TRIED to keep the list in sync, and the
    nudge is about intent, not success.
    """
    try:
        for msg in state.get("messages") or []:
            for call in getattr(msg, "tool_calls", None) or []:
                if isinstance(call, dict) and call.get("name") == "todowrite":
                    return True
    except Exception:  # noqa: BLE001 - accounting must never break the turn end
        logger.debug("plan continuation: todowrite detection failed", exc_info=True)
    return False


def _trigger_refused(result: object) -> bool:
    """True when the auto-turn trigger ANSWERED without accepting the directive.

    The server's ``maybe_trigger_auto_turn`` returns an ``AutoTurnResult`` whose
    outcome separates a queued turn (``triggered``) from a refusal (``busy`` —
    a HITL wait or another owner — and ``already_pending``). A hook that returns
    something without an outcome (a bare test spy, a differently-shaped
    implementation) counts as ACCEPTED, the pre-existing contract.
    """
    outcome = getattr(result, "outcome", None)
    if outcome is None:
        return False
    return str(getattr(outcome, "value", outcome)).lower() != "triggered"


def _is_continuation_turn(state: dict[str, Any]) -> bool:
    """True when this turn was driven by one of the gate's own directives.

    The injected directive is the turn's LAST HumanMessage (the model's answer
    and tool traffic follow it; a workspace notice or system reminder is spliced
    BEFORE the human carrier), and it carries ``origin="todo_continuation"``.
    Every other origin — a real user message, a subagent completion, a
    background-task wake — reads as False: those are new events, not
    continuations, and they reset the budget.
    """
    try:
        for msg in reversed(state.get("messages") or []):
            if isinstance(msg, HumanMessage):
                meta = getattr(msg, "metadata", None) or {}
                return str(meta.get("origin") or "") == _ORIGIN
    except Exception:  # noqa: BLE001 - accounting must never break the turn end
        logger.debug("plan continuation: continuation-turn detection failed", exc_info=True)
    return False


class PlanContinuationEnforcer(AgentMiddleware):
    """Turn-end enforcer: inject a continuation prompt when todos remain.

    Async-only BY DESIGN: this hook fires from ``aafter_agent`` alone. The
    production graph is driven through ``ainvoke``/``astream`` (the whole
    middleware chain is async), so a sync twin would only be dead code — a
    process that somehow ran the sync path would skip the gate entirely, which
    is why there is no ``after_agent`` here to half-work.

    Registered in ``agent/core.py`` FIRST in the middleware list, so it runs
    LAST at turn end (after_agent hooks execute in reverse list order).
    """

    async def aafter_agent(self, state, runtime=None) -> None:
        """Inspect the finished turn; fire-and-forget a continuation if needed."""
        try:
            # Lazy: agent.tools.todolist imports agent.middlewares at import time,
            # so a top-level import here would close the middlewares/tools cycle.
            from agent.tools.todolist import stagnation_tracker as st
            from agent.tools.todolist.registry.store_sqlite import get_todos_sync

            if not isinstance(state, dict):
                return None
            session_id = str(state.get("session_id") or "")
            if not session_id.strip():
                logger.debug("plan continuation: no session_id in state; skipping")
                return None

            # A real user turn — or a subagent completion, or a background-task
            # wake: any turn this gate did not itself inject — resets the
            # consecutive-continuation budget. The gate's own directives are
            # exactly what the budget counts, so they never reset it.
            if not _is_continuation_turn(state):
                st.reset_continuation_turns(session_id)

            # Abort-class failures (user cancel / timeout) must never continue.
            error = _turn_error(state)
            if error is not None and st.is_abort_error(error):
                logger.info("plan continuation: abort error for session {}; skipping", session_id)
                return None

            # todowrite-usage accounting: one tick per finished turn, reset by a
            # todowrite call in that turn. The nudge below uses it to catch the
            # "advancing TaskFlow, forgetting the TODO list" shape, which the
            # snapshot-based stagnation check cannot see (the board keeps moving).
            st.record_turn(session_id)
            if _turn_used_todowrite(state):
                st.record_todowrite(session_id)

            todos = get_todos_sync(session_id)
            flows = _active_flows(session_id)
            if not todos and not flows:
                st.reset(session_id)
                return None

            # Verify-todo state machine (P2): the system [VERIFY] todo is the
            # only remaining item. A running reviewer is a WAIT, not work — the
            # turn must end so the completion notice can resume the session.
            from agent.tools.todolist import service as todo_service

            verify_todo = next(
                (
                    t
                    for t in todos
                    if todo_service.is_verify_todo(t)
                    and t.get("status") in ("pending", "in_progress")
                ),
                None,
            )
            verify_only = False
            verify_state = "none"
            if verify_todo is not None:
                flow_rows = _flow_rows(flows)
                work_incomplete = any(
                    row["status"] in _INCOMPLETE_STATUSES
                    for row in [
                        {"status": t["status"]} for t in todos if not todo_service.is_verify_todo(t)
                    ]
                    + flow_rows
                )
                if not work_incomplete:
                    verify_only = True
                    if todo_service.verify_budget_exhausted(session_id):
                        # The exhausted directive was already delivered; stay
                        # silent so the model can close the plan (or cancel it).
                        return None
                    verify_state = todo_service.verify_subagent_state(verify_todo)
                    if verify_state == "running":
                        # A reviewer is live: nothing to drive. Its completion
                        # notice resumes the session; the liveness read treats a
                        # terminal/missing run as finished on a later turn.
                        return None
                    respawns = todo_service.verify_respawns(verify_todo)
                    if verify_state == "finished" and respawns >= int(
                        TODOLIST_INFRA["max_verify_respawns"]
                    ):
                        logger.info(
                            "plan continuation: verify reviewer re-spawned {}x for session {}; "
                            "offering the exhausted escape",
                            respawns,
                            session_id,
                        )
                        if await self._inject(
                            session_id,
                            _VERIFY_EXHAUSTED_PROMPT.format(todo_status=_build_status_block(todos)),
                        ):
                            await todo_service.set_verify_budget_exhausted(session_id, True)
                        return None

            # The plan = todos + the board's open flows; one snapshot drives the
            # progress check, the stagnation streak and the directive.
            plan_rows = [
                {"content": t["content"], "status": t["status"]} for t in todos
            ] + _flow_rows(flows)
            incomplete = [row for row in plan_rows if row["status"] in _INCOMPLETE_STATUSES]
            if not incomplete:
                # The plan is complete. With no open flow, spend ONE auto-turn
                # on the paranoid self-check before treating the plan as done;
                # the throttle keeps a reminder turn that ends unchanged from
                # re-arming it. A TaskFlow session skips it (its finish gates
                # already verify the goal).
                fingerprint = _plan_fingerprint(todos, _flow_rows(flows))
                if not flows and _reminded_plans.get(session_id) != fingerprint:
                    if await self._inject(session_id, _VERIFICATION_REMINDER, count_turn=False):
                        _reminded_plans[session_id] = fingerprint
                    return None
                st.reset(session_id)
                # The plan just finished: hand it to the skill-review fork.
                # Deliberately independent of compaction — a short session whose
                # window never fills must still get its plan extracted, and the
                # fork needs no conversation history (the data is in the todos,
                # the subagent runs and the plan file).
                from agent.middlewares.summarization.background_reviews import (
                    maybe_trigger_skill_review,
                )

                await maybe_trigger_skill_review(session_id)
                return None

            # The cap latched on an earlier turn: the stop directive was already
            # delivered, so the gate stays silent until a real user turn (or a
            # subagent completion) clears the latch above.
            if st.is_stopped(session_id):
                logger.info(
                    "plan continuation: session {} stopped (continuation budget spent); "
                    "waiting for a user message",
                    session_id,
                )
                return None

            flow_block = _build_flow_block(flows)
            flow_status = f"\n{flow_block}" if flow_block else ""

            # Consecutive-continuation ceiling — the shape stagnation cannot see
            # (a plan edited just enough each turn to keep the snapshot moving).
            # The gate spends ONE final directive telling the model to stop and
            # summarize; the latch then silences it. The latch is set only on a
            # CONFIRMED delivery (``_inject`` counts one), so a refused directive
            # is retried on the next turn instead of losing the stop.
            used_turns = st.continuation_turns(session_id)
            if used_turns >= st.continuation_cap():
                logger.info(
                    "plan continuation: session {} spent its continuation budget ({}/{}); "
                    "injecting the stop directive",
                    session_id,
                    used_turns,
                    st.continuation_cap(),
                )
                await self._inject(
                    session_id,
                    _STOP_PROMPT.format(
                        used=used_turns,
                        cap=st.continuation_cap(),
                        todo_status=_build_status_block(todos),
                        flow_status=flow_status,
                    ),
                )
                if st.continuation_turns(session_id) > used_turns:
                    st.mark_stopped(session_id)
                return None

            # Stagnation: unchanged across N attempts → recovery or give up.
            if st.check_stagnation(session_id, plan_rows):
                if st.should_enter_recovery(session_id):
                    st.enter_recovery(session_id)
                    logger.info(
                        "plan continuation: recovery mode for session {} ({} remaining)",
                        session_id,
                        len(incomplete),
                    )
                    await self._inject(
                        session_id,
                        _RECOVERY_PROMPT.format(
                            todo_status=_build_status_block(todos), flow_status=flow_status
                        ),
                    )
                return None

            if st.is_in_cooldown(session_id):
                logger.debug("plan continuation: session {} in cooldown; skipping", session_id)
                return None

            logger.info(
                "plan continuation: injecting continuation for session {} ({} remaining)",
                session_id,
                len(incomplete),
            )
            template = _BLOCKED_ONLY_PROMPT if _blocked_only(todos, flows) else _CONTINUATION_PROMPT
            prompt = template.format(
                todo_status=_build_status_block(todos), flow_status=flow_status
            )
            if verify_only:
                prompt += "\n\n" + _build_verify_block(verify_todo, verify_state)
            turns_without = st.turns_since_todowrite(session_id)
            if turns_without >= _TODO_WRITE_REMINDER_TURNS:
                # Appended, never a directive of its own: the model receives
                # "keep going" AND "your TODO list may be stale" in one turn.
                # Recovery mode carries its own, stronger todowrite guidance, so
                # the nudge stays out of that branch on purpose.
                prompt += _TODO_WRITE_REMINDER.format(n=turns_without)
                logger.info(
                    "plan continuation: appending the todo-tracking nudge for session {} "
                    "({} turns without todowrite)",
                    session_id,
                    turns_without,
                )
            await self._inject(session_id, prompt)
            return None
        except Exception:
            logger.exception("plan continuation: failed; continuing without injection")
            return None

    async def _inject(self, session_id: str, prompt: str, *, count_turn: bool = True) -> bool:
        """Fire-and-forget the prompt; record the injection only on success.

        The trigger is owned by the server layer and resolved through
        ``runtime.hooks``; an unregistered hook is a no-op that leaves the
        session retryable. Returns True only on a CONFIRMED delivery.
        ``count_turn=False`` keeps the final-verification reminder out of the
        continuation ceiling (it is not plan work and has its own plan throttle).
        """
        shadow_id: int | None = None
        shadow_generation: int | None = None
        try:
            from agent.tools.todolist import stagnation_tracker as st

            trigger = _resolve_hook(hooks.MAYBE_TRIGGER_AUTO_TURN)
            if trigger is None:
                return False
            session_key = f"agent:main:session:{session_id}"

            from runtime.effects import intent_log
            from runtime.effects.config import (
                effects_execution_enabled,
                effects_shadow_enabled,
            )

            prompt_digest = hashlib.sha256(prompt.encode("utf-8", "surrogateescape")).hexdigest()[
                :16
            ]
            intent_key = f"{session_id}:{st.continuation_turns(session_id)}:{prompt_digest}"

            if effects_execution_enabled():
                # Enqueue-only path: the worker performs the trigger. Marking
                # the turn here (optimistically) keeps the continuation ceiling
                # and the stop latch identical to the synchronous path; the
                # worker's delivery is independently retried.
                intent_log.enqueue(
                    kind="continuation",
                    key=intent_key,
                    payload={
                        "session_id": session_id,
                        "session_key": session_key,
                        "prompt": prompt,
                        "count_turn": count_turn,
                    },
                )
                if count_turn:
                    st.mark_injected(session_id)
                return True

            if effects_shadow_enabled():
                # Observation only: run the original path and record its
                # outcome, never changing what the user sees.
                shadow_id, shadow_generation = intent_log.enqueue(
                    kind="continuation",
                    key=intent_key,
                    payload={
                        "session_id": session_id,
                        "session_key": session_key,
                        "prompt": prompt,
                        "count_turn": count_turn,
                    },
                )

            # Injector provenance (persisted as the human row's ``origin``): these
            # directives must not read as messages the user sent.
            result = await trigger(
                session_key,
                HumanMessage(content=prompt, metadata={"origin": _ORIGIN, "internal": True}),
            )
            if _trigger_refused(result):
                # The trigger ANSWERED and declined (a HITL wait, or another
                # auto turn already queued): nothing was delivered, so nothing
                # is recorded as injected — no phantom cooldown, no continuation
                # turn counted, and the gate retries on the next turn end.
                logger.info(
                    "plan continuation: directive for session {} was refused ({})",
                    session_id,
                    getattr(result, "outcome", "unknown"),
                )
                if shadow_id is not None and shadow_generation is not None:
                    intent_log.mark_retry(shadow_id, shadow_generation, time.time() + 5.0)
                return False
            if count_turn:
                st.mark_injected(session_id)
            if shadow_id is not None and shadow_generation is not None:
                intent_log.mark_done(shadow_id, shadow_generation)
            return True
        except Exception:
            if shadow_id is not None and shadow_generation is not None:
                try:
                    intent_log.mark_retry(shadow_id, shadow_generation, time.time() + 5.0)
                except Exception:  # noqa: BLE001 - observation must never break the path
                    logger.exception("plan continuation: shadow retry mark failed")
            logger.exception(
                "plan continuation: auto_turn injection failed for session {}; "
                "leaving the session retryable",
                session_id,
            )
            return False


#: Backwards-compatible alias: the class was ``TodoContinuationEnforcer`` before
#: the rename. ``message.metadata["origin"]`` keeps the string
#: "todo_continuation", which is deliberately UNCHANGED so historical rows keep
#: resolving; this alias keeps direct imports working.
TodoContinuationEnforcer = PlanContinuationEnforcer
