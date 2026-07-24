"""Steer (redirect) a running sub-agent with new task or instructions.

Handles control-scope checks, self-steer prevention, abort-settle waiting,
suppress_announce_reason, and kill reconciliation before restart.
"""

import asyncio
import time
from loguru import logger
from ..types.registry import SubagentRunRecord, ExecutionStatus, RunOutcome, RunOutcomeStatus
from ..types.capability import ControlScope
from ..registry import (
    get_run,
    set_run,
    cancel_task,
    register_task,
    replace_run_after_steer,
    save_kill_reconciliation,
)
from ..config import get_config
from .controller import can_control_run, is_self_steer

_last_steer_at: dict[str, float] = {}  # Rate-limit tracking per caller:child pair
_ABORT_SETTLE_TIMEOUT = 5.0  # Max seconds to wait for task cancellation to settle


async def steer_subagent_run(
    run_id: str,
    new_task: str | None = None,
    new_instructions: str | None = None,
    caller_session_key: str | None = None,
) -> SubagentRunRecord | None:
    """Steer a running sub-agent by cancelling its current execution and restarting with new direction."""
    run = get_run(run_id)
    if run is None:
        logger.warning("steer_subagent_run: run {} not found", run_id)
        return None

    if run.execution.status not in (ExecutionStatus.RUNNING, ExecutionStatus.INTERRUPTED):
        logger.warning("steer_subagent_run: run {} not steerable (status={})", run_id, run.execution.status)
        return None

    if run.swarm_group_id:
        logger.warning("steer_subagent_run: cannot steer collector run {}", run_id)
        return None

    if caller_session_key:
        allowed, reason = can_control_run(run, caller_session_key)
        if not allowed:
            logger.warning("steer_subagent_run: control denied for run {}: {}", run_id, reason)
            return None

        if is_self_steer(run, caller_session_key):
            logger.warning("steer_subagent_run: self-steer rejected for run {}", run_id)
            return None

    config = get_config()
    now = time.monotonic()
    rate_key = f"{caller_session_key or 'sys'}:{run.child_session_key}"
    last_steer = _last_steer_at.get(rate_key, 0)
    if now - last_steer < config.steer_rate_limit_ms / 1000.0:
        logger.warning("steer_subagent_run: rate limited for key {}", rate_key)
        return None
    _last_steer_at[rate_key] = now

    save_kill_reconciliation(run_id)

    cancel_task(run_id)

    settled = await _abort_settle_wait(run_id, timeout=_ABORT_SETTLE_TIMEOUT)
    if not settled:
        logger.warning("steer_subagent_run: abort settle timed out for run {}", run_id)

    updated = replace_run_after_steer(run_id, new_task=new_task)
    if updated is None:
        logger.error("steer_subagent_run: replace_run_after_steer failed for {}", run_id)
        return run

    frozen_fallback = None
    if run.completion and run.completion.result_text:
        frozen_fallback = run.completion.result_text[:500]

    updated = updated.model_copy(update={
        "suppress_announce_reason": "steer-restart",  # Suppress announce for the pre-steer generation
    })
    if new_task:
        updated = updated.model_copy(update={"task": new_task})
    if frozen_fallback:
        # Preserve the previous generation's output as context for the steered run
        from ..registry.memory import update as update_run
        update_run(updated.run_id, completion=updated.completion.model_copy(update={
            "result_text": f"[FROZEN FALLBACK from previous generation]\n{frozen_fallback}",
        }))
        updated = get_run(updated.run_id) or updated

    set_run(updated)

    logger.info(
        "Steered subagent run {}: generation={}, accumulated_runtime_ms={:.0f}",
        run_id, updated.generation, updated.accumulated_runtime_ms,
    )

    steer_message = _build_steer_message(new_task, new_instructions, run)

    if frozen_fallback:
        steer_message += f"\n\n[Previous Output (for context)]\n{frozen_fallback}"

    from ..spawn.core import _build_child_agent
    from agent.tools import build_main_tools

    try:
        child_agent = await _build_child_agent(
            system_prompt=_build_steer_system_prompt(updated),
            tools=build_main_tools(),
            tool_allow=updated.inherited_tool_allow,
            tool_deny=updated.inherited_tool_deny,
            role=updated.role,
        )
    except Exception as e:
        logger.error("Failed to rebuild child agent for steer {}: {}", run_id, e)
        return updated

    restarted = updated.model_copy(update={
        "execution": updated.execution.model_copy(update={
            "status": ExecutionStatus.RUNNING,
            "started_at": time.monotonic(),
        }),
        "pause_reason": None,
        "suppress_announce_reason": None,
    })
    set_run(restarted)

    timeout_seconds = config.run_timeout_seconds
    bg_task = asyncio.create_task(
        _execute_steered_subagent(
            run=restarted,
            child_agent=child_agent,
            steer_message=steer_message,
            timeout_seconds=timeout_seconds,
        )
    )

    from ..registry import register_task as _register_task
    _register_task(run_id, bg_task)

    return restarted


async def _abort_settle_wait(run_id: str, timeout: float = 5.0) -> bool:
    """Wait for the current task to settle after cancellation, up to the given timeout."""
    from ..registry import get_task
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = get_task(run_id)
        if task is None or task.done():
            return True
        await asyncio.sleep(0.1)
    return False


async def _execute_steered_subagent(
    run: SubagentRunRecord,
    child_agent,
    steer_message: str,
    timeout_seconds: float,
) -> None:
    """Run the steered sub-agent to completion, handling timeout/cancel/error outcomes."""
    from langchain_core.messages import HumanMessage

    result_text: str | None = None
    outcome = RunOutcome(status=RunOutcomeStatus.OK)

    try:
        from pub_func import build_agent_config

        agent_result = await asyncio.wait_for(
            child_agent.ainvoke(
                input={
                    "session_id": run.child_session_key,
                    "messages": [HumanMessage(content=steer_message)],
                },
                config=build_agent_config(session_id=run.child_session_key),
            ),
            timeout=timeout_seconds,
        )

        if agent_result and "messages" in agent_result:
            last_msg = agent_result["messages"][-1] if agent_result["messages"] else None
            if last_msg and hasattr(last_msg, "content"):
                result_text = last_msg.content if isinstance(last_msg.content, str) else str(last_msg.content)

    except asyncio.TimeoutError:
        outcome = RunOutcome(status=RunOutcomeStatus.TIMEOUT, error=f"Steered subagent timed out after {timeout_seconds}s")
    except asyncio.CancelledError:
        outcome = RunOutcome(status=RunOutcomeStatus.KILLED, error="Steered subagent was killed")
    except Exception as e:
        outcome = RunOutcome(status=RunOutcomeStatus.ERROR, error=str(e))
    finally:
        from ..registry import remove_task
        remove_task(run.run_id)

        from ..registry.lifecycle import complete_subagent_run
        await complete_subagent_run(run.run_id, outcome, result_text)


def _build_steer_message(new_task: str | None, new_instructions: str | None, original_run: SubagentRunRecord) -> str:
    """Compose the human-readable steer message sent to the sub-agent."""
    parts = ["[STEER] Your task has been redirected by your parent agent.\n"]

    if new_task:
        parts.append(f"NEW TASK:\n{new_task}")
    else:
        parts.append(f"ORIGINAL TASK (unchanged):\n{original_run.task}")

    if new_instructions:
        parts.append(f"\nADDITIONAL INSTRUCTIONS:\n{new_instructions}")

    parts.append("\nPlease adjust your approach accordingly and continue.")
    return "\n\n".join(parts)


def _build_steer_system_prompt(run: SubagentRunRecord) -> str:
    """Build the system prompt for a steered sub-agent, including role and session context."""
    from ..types.capability import SubagentSessionRole

    base = (
        "You are a focused subagent working on a specific delegated task.\n"
        f"You were spawned by {run.requester_session_key}.\n"
        "Focus ONLY on the assigned task. Do not take proactive actions beyond your task scope.\n"
        "When done, report your results concisely and finish.\n"
    )

    if run.role == SubagentSessionRole.LEAF:
        # LEAF workers cannot spawn further sub-agents
        role_rules = (
            "\nYou are a LEAF worker — you CANNOT spawn further subagents.\n"
            "Execute your task directly and report back.\n"
        )
    elif run.role == SubagentSessionRole.ORCHESTRATOR:
        # ORCHESTRATOR workers can spawn, yield, and kill child sub-agents
        role_rules = (
            "\nYou are an ORCHESTRATOR — you MAY spawn further subagents using sessions_spawn.\n"
            "Use sessions_yield to wait for children to complete.\n"
            "Use sessions_kill to cancel subagents that are no longer needed.\n"
        )
    else:
        role_rules = ""

    context_section = ""
    if run.child_session_key:
        context_section = (
            f"\nSession context:\n"
            f"  Your session key: {run.child_session_key}\n"
            f"  Parent session key: {run.requester_session_key}\n"
            f"  Depth: {run.depth}\n"
            f"  Generation: {run.generation}\n"
        )

    return base + role_rules + context_section
