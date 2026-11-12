"""Run lifecycle: load gate, lane execution, goal loop, cleanup and finalization.

This is everything that happens AFTER a spawn has been accepted: the deferred
dispatch waits out the host-load gate, takes a SUBAGENT lane slot, promotes the
run PENDING → RUNNING, drives the child agent (with the optional goal loop), and
always releases/finalizes the run — on success, timeout, cancellation or error.

The collaborators the facade (``spawn.core``) re-exports are passed in through
:class:`ExecutionSeams`: the test-suite monkeypatches them on ``spawn.core`` and
expects the run to pick the replacement up, so the bundle is rebuilt from the
facade's module globals at every entry. A direct call (``seams=None``) uses this
module's own functions.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from config.features import LANE_SYSTEM
from ..hooks.progress import fire_ended_hook, fire_progress_hook
from ..output_caps import resolve_subagent_output_caps
from ..registry import get_run, mark_run_running, remove_task
from ..session.cleanup import delete_subagent_session_for_cleanup
from ..types.registry import (
    ExecutionStatus,
    RunOutcome,
    RunOutcomeStatus,
    SubagentRunRecord,
)
from .child_agent_factory import (
    _bind_child_run_context,
    _build_child_agent,
    _child_model_identity,
    _effective_child_deny,
    _is_structured_output_rejection,
)
from .handoff import _extract_handoff_json, _extract_result_text
from .ptc_prefill import _try_ptc_prefill


@dataclass(frozen=True)
class ExecutionSeams:
    """The execution loop's collaborators, resolved by the facade per call.

    ``spawn.core`` keeps the historical single-module surface the tests drive
    (they monkeypatch ``_build_child_agent`` / ``_execute_subagent`` /
    ``_finalize_child_run`` THERE), so the facade hands a bundle built from its
    own module globals to every entry point. Production callers pass nothing
    and this module's own functions are used.
    """

    execute_subagent: Callable[..., Awaitable[None]]
    build_child_agent: Callable[..., Awaitable[Any]]
    try_ptc_prefill: Callable[..., Awaitable[tuple[dict[str, Any] | None, str | None]]]
    bind_child_run_context: Callable[[SubagentRunRecord], dict]
    run_goal_loop: Callable[..., Awaitable[tuple[str | None, RunOutcome | None]]]
    release_child_resources: Callable[..., Awaitable[None]]
    finalize_child_run: Callable[..., Awaitable[None]]


def default_seams() -> ExecutionSeams:
    """The bundle used when no facade supplied its (patchable) replacements."""
    return ExecutionSeams(
        execute_subagent=_execute_subagent,
        build_child_agent=_build_child_agent,
        try_ptc_prefill=_try_ptc_prefill,
        bind_child_run_context=_bind_child_run_context,
        run_goal_loop=_run_goal_loop,
        release_child_resources=_release_child_resources,
        finalize_child_run=_finalize_child_run,
    )


#: Runs already notified about a deferred dispatch. Module-level on purpose: the
#: registry record has no generic metadata slot, and the throttle only has to
#: survive this process. Cleaned when the run leaves the lane wrapper.
_load_warned_runs: set[str] = set()


def _is_run_killed(run: SubagentRunRecord) -> bool:
    """Whether a deferred dispatch should stop waiting.

    A PENDING run that is killed does NOT become TERMINAL (the kill path only
    records ``kill_reconciliation``), so checking the status alone would keep
    waiting for a run nobody will ever execute.
    """
    return run.execution.status == ExecutionStatus.TERMINAL or run.kill_reconciliation is not None


def _fail_run_load_timeout(run: SubagentRunRecord, gate: Any) -> None:
    """Terminalize a dispatch the load gate never let through.

    MUST terminalize, not merely return: an abandoned PENDING run is an orphan,
    and the sweeper's orphan recovery would re-dispatch it → another deferral →
    a loop. ``ended_reason`` comes from the outcome status (``timeout``), and the
    load-specific detail rides the outcome's error text.
    """
    from ..registry.run_manager import complete_run

    complete_run(
        run.run_id,
        RunOutcome(
            status=RunOutcomeStatus.TIMEOUT,
            error=(
                f"load_timeout: host load stayed above the gate for more than "
                f"{LANE_SYSTEM['load_gate_max_defer_s']:.0f}s "
                f"(cpu {gate.cpu:.0f}%, ram {gate.ram:.0f}%)"
            ),
        ),
    )
    logger.warning(
        "subagent run {} terminalized: deferred by the load gate past the budget", run.run_id
    )


async def _notify_load_warning(run: SubagentRunRecord, gate: Any) -> None:
    """Push the ``load_warning`` frame for this run ONCE (best effort)."""
    if run.run_id in _load_warned_runs:
        return
    _load_warned_runs.add(run.run_id)
    try:
        from runtime import hooks

        broadcast_to_session = hooks.resolve(hooks.WS_PUSH)
        if broadcast_to_session is None:
            # Compatibility fallback for a process without the server assembly
            # (unit tests patch this module-level function directly).
            from runtime.session.ws_push import broadcast_to_session

        frame = {
            "event": "load_warning",
            "session_id": run.requester_session_key,
            "content": {
                "cpu": round(gate.cpu, 1),
                "ram": round(gate.ram, 1),
                "run_id": run.run_id,
            },
        }
        await broadcast_to_session(run.requester_session_key, frame)
    except Exception as e:  # noqa: BLE001 — a notice must never block the wait
        logger.warning("Failed to push load_warning for run {}: {}", run.run_id, e)


async def _wait_for_load_gate(run: SubagentRunRecord) -> bool:
    """Wait out a stop verdict BEFORE taking a lane slot. False = give up.

    Placed in front of ``lane_slot`` on purpose: a deferred dispatch must not
    consume a slot it will not use. The wait reads the process-wide snapshot
    (no per-run sampling), backs off ``load_gate_retry_s`` at a time, and gives
    up either when the run is killed or when the deferral budget is spent (the
    run is then terminalized — see :func:`_fail_run_load_timeout`).
    """
    if not LANE_SYSTEM["load_gate_enabled"]:
        return True  # shipped-off: byte-for-byte the old behavior, no sampling

    from agent.tools.pub_base.load_gate import get_load_gate

    defer_started = time.monotonic()
    while True:
        gate = await get_load_gate()
        if not gate.stop:
            return True
        elapsed = time.monotonic() - defer_started
        budget = float(LANE_SYSTEM["load_gate_max_defer_s"])
        if elapsed >= budget:
            _fail_run_load_timeout(run, gate)
            return False
        logger.info(
            "subagent run {} deferred by the load gate (cpu={:.0f}% ram={:.0f}%)",
            run.run_id,
            gate.cpu,
            gate.ram,
        )
        await _notify_load_warning(run, gate)
        # Never sleep past the budget: a short max_defer must be observed on time.
        await asyncio.sleep(min(float(LANE_SYSTEM["load_gate_retry_s"]), budget - elapsed))
        current = get_run(run.run_id)
        if current is None or _is_run_killed(current):
            logger.info("subagent run {} killed while deferred by the load gate", run.run_id)
            return False


async def _execute_subagent_with_lane(
    run: SubagentRunRecord,
    system_prompt: str,
    user_message: str,
    tools: list | None,
    timeout_seconds: float,
    model_override: str | None = None,
    model_tier: str | None = None,
    model_profile: dict[str, str] | None = None,
    extra_tools: list[str] | None = None,
    output_schema: dict | None = None,
    goal_max_turns: int = 5,
    *,
    seams: ExecutionSeams | None = None,
) -> None:
    """Wait for a SUBAGENT lane slot, promote PENDING → RUNNING inside it, then execute.

    The lane queues over-limit runs instead of rejecting them: a spawn that
    already passed per-parent admission always proceeds, waiting here while all
    slots are busy. ``started_at`` is stamped only once the slot is held, so
    queue wait time is never counted as run time.
    """
    from runtime.lane import LaneType, lane_slot

    seams = seams or default_seams()
    try:
        # The environment gate runs FIRST: a deferred dispatch holds no slot.
        if not await _wait_for_load_gate(run):
            return
        logger.debug("subagent run {} waiting for a SUBAGENT lane slot", run.run_id)
        async with lane_slot(LaneType.SUBAGENT):
            current = get_run(run.run_id)
            if current is None or current.execution.status == ExecutionStatus.TERMINAL:
                logger.info("subagent run {} killed while waiting for a lane slot", run.run_id)
                return  # killed while waiting for a lane slot

            if current.execution.status == ExecutionStatus.PENDING:
                promoted = mark_run_running(run.run_id)
                if promoted is None:
                    logger.warning("subagent run {} vanished before PENDING -> RUNNING", run.run_id)
                    return  # race: run disappeared between lookup and transition
                current = promoted
                logger.info("subagent run {} PENDING -> RUNNING inside its lane slot", run.run_id)

            await seams.execute_subagent(
                run=current,
                system_prompt=system_prompt,
                user_message=user_message,
                tools=tools,
                timeout_seconds=timeout_seconds,
                model_override=model_override,
                model_tier=model_tier,
                model_profile=model_profile,
                extra_tools=extra_tools,
                output_schema=output_schema,
                goal_max_turns=goal_max_turns,
            )
    finally:
        # Early return / cancellation paths never reach _execute_subagent's own
        # cleanup, so the task reference is released here exactly once.
        _load_warned_runs.discard(run.run_id)
        remove_task(run.run_id)


class _RunWallClock:
    """One wall-clock budget shared by every turn a run makes.

    ``run_timeout_seconds`` bounds the RUN, not each model call: the goal loop
    may make ``goal_max_turns`` calls, and a per-call ``wait_for(timeout)``
    multiplied the advertised bound by the turn budget. ``remaining()`` returns
    the seconds left for the next call (``None`` when the budget is disabled by
    ``timeout_seconds <= 0``) and raises ``TimeoutError`` once it is spent, so
    the caller's normal timeout path finalizes the run as TIMEOUT.
    """

    def __init__(self, timeout_seconds: float) -> None:
        self.deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None

    def remaining(self) -> float | None:
        if self.deadline is None:
            return None
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return remaining


async def _reset_child_attempt(run: SubagentRunRecord, child_agent: Any) -> None:
    """Prepare a clean retry after a rejected structured-output attempt.

    Closes the failed attempt's checkpointer, wipes the child's checkpoint
    thread (the failed turn may have saved the input state — replaying it would
    duplicate the HumanMessage), and releases the repetition-guard keys so the
    fresh attempt starts from zero. Best-effort: a cleanup failure must not
    block the retry.
    """
    child_checkpointer = getattr(child_agent, "checkpointer", None) if child_agent else None
    if child_checkpointer is not None:
        try:
            await child_checkpointer.aclose()
        except Exception:
            logger.debug("subagent retry: checkpointer close failed", exc_info=True)
    try:
        from agent.checkpointer.async_sqlite_checkpointer import delete_thread_history

        await delete_thread_history(run.child_session_key)
    except Exception:
        logger.debug("subagent retry: thread wipe failed", exc_info=True)
    try:
        # Same-module helper (the registry copy does not exist).
        release_child_session_state(run)
    except Exception:
        logger.debug("subagent retry: session state release failed", exc_info=True)


def release_child_session_state(run: SubagentRunRecord) -> None:
    """Drop the per-session state a child agent leaves in the mem register.

    Split out of :func:`_release_child_resources` because a run that is killed
    while INTERRUPTED (paused on HITL, possibly restored from disk after a
    restart) has no live child agent to release — but its OutputRepetitionGuard
    keys are still in the child's bucket and would leak once per run.
    """
    try:
        from agent.middlewares.output_repetition_guard.repetition_state import (
            SESSION_STATE_KEYS,
        )
        from runtime import state_register_mem

        for _key in SESSION_STATE_KEYS:
            state_register_mem.delete_state(run.child_session_key, _key)
    except Exception:
        logger.exception(
            "Failed to release OutputRepetitionGuard state for session {}",
            run.child_session_key,
        )


async def _release_child_resources(run: SubagentRunRecord, child_agent: Any) -> None:
    """Close the child's checkpointer and drop its per-session state.

    Always runs, even on cancellation: every spawn opens an aiosqlite
    checkpointer and writes repetition-guard keys under the child's session
    bucket, and nothing else would release them.
    """
    child_checkpointer = getattr(child_agent, "checkpointer", None)
    if child_checkpointer is not None:
        # Keep this child thread to one readable top-level checkpoint (plus its
        # writes). The child is no longer running here; tools/model namespaces
        # are safe to drop, while the latest top-level row is what
        # sessions_messages reads after a timeout/kill.
        cleaner = getattr(child_checkpointer, "aclean_thread_checkpoints", None)
        if callable(cleaner):
            try:
                from pub.func import rand_str_to_int

                await cleaner(str(rand_str_to_int(run.child_session_key)))
            except Exception:
                logger.warning(
                    "Failed to prune child checkpoints for {}",
                    run.run_id,
                    exc_info=True,
                )
        try:
            await child_checkpointer.aclose()
        except Exception:
            logger.exception("Failed to close child checkpointer for {}", run.run_id)

    # Release this child's per-session OutputRepetitionGuard state. The guard
    # writes its top-level keys (output_repetition_history, ..._halted, etc.)
    # into ``state_register_mem`` under the child's unique session key. If we
    # left them behind they would accumulate in memory each time a subagent
    # is spawned and destroyed. ``clear_session`` is deliberately NOT used
    # here: it would wipe the entire session bucket and clobber the other
    # middlewares' top-level state (heartbeat_killed, summarization, tool
    # guardrails, iteration budget, ...). We delete exactly the 6 keys this
    # middleware owns, scoped to the child's own bucket via ``delete_state``.
    release_child_session_state(run)

    remove_task(run.run_id)

    try:
        await fire_progress_hook(run, "execution completed")
    except Exception as e:
        logger.debug("fire_progress_hook error for run {}: {}", run.run_id, e)


async def _finalize_child_run(
    run: SubagentRunRecord,
    outcome: RunOutcome,
    result_text: str | None,
    handoff: dict | None = None,
) -> None:
    """Grace period (failures only), then complete the run and fire the hooks."""
    if outcome.status in (RunOutcomeStatus.ERROR, RunOutcomeStatus.TIMEOUT):
        from ..config import get_config

        grace = get_config().lifecycle_grace_period_seconds
        if grace > 0:
            # Give in-flight completion messages a chance to arrive before finalizing
            logger.info(
                "Lifecycle grace: waiting {:.1f}s before finalizing run {} ({})",
                grace,
                run.run_id,
                outcome.status.value,
            )
            await asyncio.sleep(grace)
            from ..registry import get_run as _get_run

            latest = _get_run(run.run_id)
            if latest and latest.execution.status == ExecutionStatus.TERMINAL:
                # Run was already completed (e.g. by a yield/settle callback) during grace period
                logger.info(
                    "Lifecycle grace: run {} completed during grace period, skipping finalize",
                    run.run_id,
                )
                try:
                    await fire_ended_hook(run)
                except Exception as e:
                    logger.debug("fire_ended_hook error for run {}: {}", run.run_id, e)
                return

    from ..registry.lifecycle import complete_subagent_run

    await complete_subagent_run(
        run.run_id,
        outcome,
        result_text,
        expected_generation=run.generation,
        handoff=handoff,
    )

    try:
        await fire_ended_hook(run)
    except Exception as e:
        logger.debug("fire_ended_hook error for run {}: {}", run.run_id, e)


async def _execute_subagent(
    run: SubagentRunRecord,
    system_prompt: str,
    user_message: str,
    tools: list | None,
    timeout_seconds: float,
    model_override: str | None = None,
    model_tier: str | None = None,
    model_profile: dict[str, str] | None = None,
    extra_tools: list[str] | None = None,
    output_schema: dict | None = None,
    *,
    goal_max_turns: int = 5,
    seams: ExecutionSeams | None = None,
) -> None:
    """Run a sub-agent under its OWN log scope (see :func:`_execute_subagent_scoped`).

    The child runs in an asyncio task that inherits whatever ``contextualize``
    scope the parent turn opened, so this wrapper rebinds the child's identity:
    its log lines carry the child session key and a fresh turn id, never the
    parent's, and one subagent run is greppable on its own key.
    """
    with logger.contextualize(
        session_id=run.child_session_key,
        turn_id=uuid.uuid4().hex,
        agent="subagent",
    ):
        await _execute_subagent_scoped(
            run,
            system_prompt=system_prompt,
            user_message=user_message,
            tools=tools,
            timeout_seconds=timeout_seconds,
            model_override=model_override,
            model_tier=model_tier,
            model_profile=model_profile,
            extra_tools=extra_tools,
            output_schema=output_schema,
            goal_max_turns=goal_max_turns,
            seams=seams,
        )


async def _run_goal_loop(
    run: SubagentRunRecord,
    user_message: str,
    result_text: str | None,
    invoke: Any,
    goal_max_turns: int,
) -> tuple[str | None, RunOutcome | None]:
    """Review each turn with the completion judge until DONE or budget spent.

    CONTINUE injects the judge's continuation prompt as the next human turn.
    Fail-open (judge errors => DONE) so it cannot trap the subagent. Returns the
    latest result text and an outcome only when the budget ran out.

    The judge's FIRST call is skipped when the child's opening turn already
    cleared the bar on its own: a non-empty result plus at least one passing,
    non-stale evidence row in the child's ledger (``_first_turn_self_evident``).
    The point: a simple spawn should not pay an auxiliary LLM call for a verdict
    the ledger already carries.
    """
    from agent.tools.taskflow.evidence_collector import collect_evidence_summary

    from .completion_judge import CompletionVerdict, judge_completion

    turns_used = 1
    while True:
        evidence_summary = collect_evidence_summary(session_key=run.child_session_key)
        if turns_used == 1 and _first_turn_self_evident(result_text, evidence_summary):
            logger.debug(
                "CompletionJudge skipped for run {}: first turn carries passing evidence",
                run.run_id,
            )
            return result_text, None
        judge_result = await judge_completion(
            task_text=user_message,
            last_response=result_text or "",
            evidence_summary=evidence_summary,
            max_chars=resolve_subagent_output_caps(run.max_output_tokens).judge_chars,
        )
        if judge_result.verdict == CompletionVerdict.DONE:
            return result_text, None
        if turns_used >= goal_max_turns:
            return result_text, RunOutcome(
                status=RunOutcomeStatus.BUDGET_EXHAUSTED,
                error="goal_loop_budget_exhausted",
            )
        if not judge_result.continuation_prompt.strip():
            return result_text, None
        from langchain_core.messages import HumanMessage

        agent_result = await invoke([HumanMessage(content=judge_result.continuation_prompt)])
        result_text = _extract_result_text(agent_result)
        turns_used += 1


def _first_turn_self_evident(result_text: str | None, evidence_summary: str | None) -> bool:
    """True when the opening turn needs no judge: result + passing evidence.

    The summary format is the evidence collector's (``[pass]`` / ``[FAIL]`` /
    ``?`` per row, `` [stale]`` / `` [broken]`` suffixed): a pass exists, nothing
    failed, nothing went stale, and no command was re-verified as broken. Any
    read failure returns False — the shortcut must never suppress a judge on
    incomplete information (fail towards judging).
    """
    if not (result_text or "").strip():
        return False
    if not evidence_summary or "[pass]" not in evidence_summary:
        return False
    return (
        "FAIL" not in evidence_summary
        and "[stale]" not in evidence_summary
        and "[broken]" not in evidence_summary
    )


async def _execute_subagent_scoped(
    run: SubagentRunRecord,
    system_prompt: str,
    user_message: str,
    tools: list | None,
    timeout_seconds: float,
    model_override: str | None = None,
    model_tier: str | None = None,
    model_profile: dict[str, str] | None = None,
    extra_tools: list[str] | None = None,
    output_schema: dict | None = None,
    *,
    goal_max_turns: int = 5,
    seams: ExecutionSeams | None = None,
) -> None:
    """Run a sub-agent to completion, handling timeouts, cancellation, and lifecycle cleanup.

    This is the main execution loop for a spawned sub-agent. It:
      1. Computes the effective tool deny-list from inherited policy + scope gaps.
      2. Builds a child LangGraph agent with filtered tools and role-appropriate LLM.
      3. Invokes the agent with the assembled messages under a wall-clock timeout.
      4. (goal loop) Reviews each turn with the completion judge; CONTINUE injects a
         continuation prompt and runs another turn until DONE or the budget is spent.
         Runs for every spawn whose budget exceeds one turn.
      5. Validates structured output (if an output_schema was provided).
      6. On failure (timeout / kill / error), applies a configurable grace period
         to allow in-flight completion messages to arrive before finalizing.
      7. Always runs lifecycle cleanup (thread unbinding, task removal, run completion).

    Args:
        run: The registry record for this spawn.
        system_prompt: System prompt injected into the child agent.
        user_message: The initial human-message containing the task.
        tools: Tool list to make available; ``None`` falls back to :func:`build_main_tools`.
        timeout_seconds: Wall-clock timeout in seconds.
        model_override: LLM model name override for this child.
        output_schema: Optional JSON Schema for structured output validation.
        goal_max_turns: Goal-loop turn budget including the first turn.
    """
    from langchain_core.messages import HumanMessage

    seams = seams or default_seams()
    effective_deny = _effective_child_deny(run)

    # Optimistic default — overridden by the appropriate exception handler below
    result_text: str | None = None
    handoff: dict | None = None
    outcome = RunOutcome(status=RunOutcomeStatus.OK)
    # Bound before the try so the cleanup below can always see it, even when
    # _build_child_agent itself raised (e.g. a missing required middleware).
    child_agent = None

    from .. import model_caps

    identity = (
        _child_model_identity(model_profile, run.functional_role, model_tier, run.role)
        if output_schema
        else ("", "")
    )
    # A model that already degraded starts at its learned level, so a later
    # subagent never pays for the same rejected request again.
    strategy_level = model_caps.get_level(*identity) if output_schema else 0
    strategy = model_caps.strategy_for(strategy_level)
    if output_schema and strategy_level:
        logger.info(
            "Subagent structured output: {} starts at level {} ({})",
            "/".join(identity),
            strategy_level,
            strategy.label,
        )

    # ONE wall-clock budget for the whole run, retries included: degrading a
    # strategy must not multiply the advertised run timeout.
    budget = _RunWallClock(timeout_seconds)

    try:
        # System-level PTC replay runs before any child agent is built. A hit
        # completes the step with zero LLM tokens; a miss falls through to React
        # with the completed prefix in its initial message.
        ptc_result, ptc_progress = await seams.try_ptc_prefill(
            run, tools, effective_deny, output_schema
        )
        if ptc_result is not None:
            result_text = ptc_result["output"]
            handoff = None
            outcome = RunOutcome(status=RunOutcomeStatus.OK)
            return
        if ptc_progress:
            user_message = f"{user_message}\n\n{ptc_progress}"

        while True:
            # Build the child LangGraph agent with scoped tools
            child_agent = await seams.build_child_agent(
                system_prompt=system_prompt,
                tools=tools,
                tool_allow=run.inherited_tool_allow,
                tool_deny=effective_deny,
                role=run.role,
                functional_role=run.functional_role,
                model_override=model_override,
                model_tier=model_tier,
                model_profile=model_profile,
                extra_tools=extra_tools,
                session_id=run.child_session_key,
                output_schema=output_schema,
                strategy_level=strategy_level,
                spawn_depth=run.depth,
            )

            messages = [HumanMessage(content=user_message)]

            agent_config = seams.bind_child_run_context(run)

            # Invoke the child agent under the wall-clock timeout (0 disables it).
            # ``_invoke`` is reused by goal-loop continuation turns: the same
            # config keeps the same checkpoint thread, so a continuation extends
            # the SAME child conversation instead of starting a fresh one.
            async def _invoke(msgs: list) -> dict:
                payload = {"session_id": run.child_session_key, "messages": msgs}
                remaining = budget.remaining()
                if remaining is None:
                    return await child_agent.ainvoke(input=payload, config=agent_config)
                return await asyncio.wait_for(
                    child_agent.ainvoke(input=payload, config=agent_config),
                    timeout=remaining,
                )

            try:
                agent_result = await _invoke(messages)
            except Exception as exc:  # noqa: BLE001 - a rejection is classified below
                if (
                    output_schema is not None
                    and strategy_level < model_caps.MAX_LEVEL
                    and _is_structured_output_rejection(exc)
                ):
                    # Degrade ONE rung and retry from a clean conversation. The
                    # level is persisted, so the NEXT child on this model starts
                    # here instead of re-hitting the same 400.
                    strategy_level = model_caps.escalate(
                        *identity,
                        strategy_level + 1,
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                    strategy = model_caps.strategy_for(strategy_level)
                    logger.warning(
                        "Subagent structured output rejected for {} ({}); retrying at level {} ({})",
                        run.run_id,
                        type(exc).__name__,
                        strategy_level,
                        strategy.label,
                    )
                    await _reset_child_attempt(run, child_agent)
                    child_agent = None
                    continue
                raise
            break

        # The model accepted this rung: if get_level had decayed it, commit the
        # lower level now (a spawn at the persisted level is a no-op, so steady
        # use cannot pin a degradation forever).
        if output_schema is not None:
            model_caps.confirm_level(*identity, strategy_level)

        # Extract the last substantive assistant text as the result text.
        # The last message in the returned state is frequently an AIMessage with
        # empty content (only tool_calls) or a ToolMessage — both carry tool output,
        # not the final answer. Walk backwards to the last assistant message that
        # contains real text, flattening multimodal content-block lists.
        result_text = _extract_result_text(agent_result)

        # Goal loop: the completion judge reviews every turn; CONTINUE injects its
        # continuation prompt as the next human turn until DONE or the budget is
        # spent. Fail-open (judge errors => DONE) so it cannot trap the subagent.
        # The master switch is read HERE, at the funnel every execution path
        # passes through (spawn, steer, direct calls): disabled ends the loop
        # whatever budget the caller resolved.
        from config.features import COMPLETION_JUDGE as _CJ

        if not _CJ.get("enabled", True):
            if goal_max_turns > 1:
                logger.debug(
                    "CompletionJudge disabled: run {} executes without a goal loop", run.run_id
                )
            goal_max_turns = 1
        if goal_max_turns > 1:
            result_text, budget_outcome = await seams.run_goal_loop(
                run, user_message, result_text, _invoke, goal_max_turns
            )
            if budget_outcome is not None:
                outcome = budget_outcome

        # Validate structured output against the provided JSON Schema. At the
        # self-json-object rungs nothing else enforces the shape, so a miss
        # FAILS the run (the caller's retry flow owns the next attempt); the
        # LangChain rung keeps the legacy warn-only behaviour because the
        # framework already enforced it.
        if output_schema and result_text:
            from ..swarm.collector import validate_structured_output

            valid, err = validate_structured_output(result_text, output_schema)
            if not valid:
                if strategy.local_validation_fatal and outcome.status is RunOutcomeStatus.OK:
                    outcome = RunOutcome(
                        status=RunOutcomeStatus.ERROR,
                        error=(
                            f"Structured output validation failed: {err}; got {result_text[:160]!r}"
                        ),
                    )
                else:
                    logger.warning(
                        "Structured output validation failed for run {}: {}", run.run_id, err
                    )

        # The layer's own handoff block, extracted ONCE here and stored with the
        # run: skill review reads the parsed dict instead of re-parsing
        # free text 24 KB at a time.
        handoff = _extract_handoff_json(result_text)

    # --- Exception handling: map failures to RunOutcome statuses ---
    except TimeoutError:
        outcome = RunOutcome(
            status=RunOutcomeStatus.TIMEOUT, error=f"Subagent timed out after {timeout_seconds}s"
        )
    except asyncio.CancelledError:
        outcome = RunOutcome(status=RunOutcomeStatus.KILLED, error="Subagent was killed")
    except Exception as e:
        outcome = RunOutcome(status=RunOutcomeStatus.ERROR, error=str(e))

    # --- Lifecycle cleanup (always runs, even on cancellation) ---
    finally:
        await seams.release_child_resources(run, child_agent)
        await seams.finalize_child_run(run, outcome, result_text, handoff)


async def _rollback_spawn(
    child_session_key: str,
    run_id: str | None,
    attachments_dir: str | None,
    attachments_root_dir: str | None,
) -> None:
    """Clean up partial spawn state (attachments, session, registry) after a failed spawn.

    Performs three cleanup steps in order:
      1. Remove materialised attachment files from disk.
      2. Delete the child session entry.
      3. Remove the registry run record (if one was created) and fire stop hooks.

    Args:
        child_session_key: Session key of the failed child.
        run_id: Run identifier if registration succeeded before the failure.
        attachments_dir: Absolute path to the attachments directory (if created).
        attachments_root_dir: Root directory under which attachments live.
    """
    if attachments_dir:
        from ..registry.helpers import safe_remove_attachments_dir

        safe_remove_attachments_dir(attachments_dir, attachments_root_dir)

    await delete_subagent_session_for_cleanup(child_session_key)

    if run_id:
        from ..registry import remove_run as _remove_run

        try:
            await _remove_run(run_id)
        except Exception as e:
            logger.debug("remove_run failed for run {}: {}", run_id, e)

        from ..hooks.base import fire_stop_hooks

        try:
            await fire_stop_hooks(
                SubagentRunRecord(
                    run_id=run_id,
                    child_session_key=child_session_key,
                    requester_session_key="",
                    task="",
                )
            )
        except Exception as e:
            logger.debug("fire_stop_hooks error for run {}: {}", run_id, e)
