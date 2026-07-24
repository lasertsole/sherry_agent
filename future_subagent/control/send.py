"""Send messages to running sub-agents (sessions_send).

Captures a baseline reply, dispatches the message, and optionally waits for
an updated reply from the sub-agent.
"""

import asyncio
import time
from loguru import logger
from ..types.registry import SubagentRunRecord, ExecutionStatus
from ..registry import get_run, set_run
from .controller import can_control_run


async def send_subagent_message(
    run_id: str,
    message: str,
    caller_session_key: str,
    wait_for_reply: bool = True,
    timeout_seconds: float = 30.0,
) -> str | None:
    """Send a message to a running sub-agent and optionally wait for its reply."""
    run = get_run(run_id)
    if run is None:
        return None

    allowed, reason = can_control_run(run, caller_session_key)
    if not allowed:
        logger.warning("send_subagent_message: control denied for run {}: {}", run_id, reason)
        return None

    if run.execution.status not in (ExecutionStatus.RUNNING, ExecutionStatus.INTERRUPTED):
        logger.warning("send_subagent_message: run {} not in sendable state ({})", run_id, run.execution.status)
        return None

    baseline = await _capture_baseline_reply(run.child_session_key)

    try:
        from bus import MessageBus
        from type.bus import InboundMessage

        msg = InboundMessage(
            channel="system",
            sender_id=caller_session_key,
            chat_id="direct",
            content=message,
            session_id=run.child_session_key,
            metadata={
                "injected_event": "subagent_send",
                "subagent_run_id": run.run_id,
            },
        )

        bus = MessageBus()
        await bus.publish_inbound(msg)
        logger.info("Sent message to subagent run {}: {} chars", run_id, len(message))
    except Exception as e:
        logger.error("Failed to send message to subagent run {}: {}", run_id, e)
        return None

    if not wait_for_reply:
        return None

    return await _wait_for_updated_reply(run.child_session_key, baseline, timeout_seconds)


async def _capture_baseline_reply(child_session_key: str) -> str | None:
    """Snapshot the current last AI message so we can detect when a new reply arrives."""
    try:
        from agent import built_agent
        from pub_func import build_agent_config

        agent = await built_agent()
        state = await agent.aget_state(config=build_agent_config(child_session_key))
        messages = state.values.get("messages", [])

        for msg in reversed(messages):
            if hasattr(msg, "type") and msg.type == "ai":
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                return content[:500]
        return None
    except Exception:
        return None


async def _wait_for_updated_reply(
    child_session_key: str,
    baseline: str | None,
    timeout_seconds: float,
) -> str | None:
    """Poll the agent state until the last AI message differs from the baseline."""
    deadline = time.monotonic() + timeout_seconds
    poll_interval = 0.5  # Check every 500ms for a new reply

    while time.monotonic() < deadline:
        await asyncio.sleep(poll_interval)

        try:
            from agent import built_agent
            from pub_func import build_agent_config

            agent = await built_agent()
            state = await agent.aget_state(config=build_agent_config(child_session_key))
            messages = state.values.get("messages", [])

            for msg in reversed(messages):
                if hasattr(msg, "type") and msg.type == "ai":
                    content = msg.content if isinstance(msg.content, str) else str(msg.content)
                    if content != (baseline or ""):
                        return content
                    break
        except Exception:
            pass

    return None
