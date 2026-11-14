"""Handlers for the three effect kinds the worker can execute.

Each handler is idempotent on its own: continuation re-checks the trigger's
outcome, completion re-enters the existing delivery path (which checks the
registry's delivered flag first), and auto-turn re-runs the normal busy check.
A permanent condition abandons; a transient one returns a retry hint.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage
from loguru import logger

from runtime.effects.worker import EffectResult

__all__ = ["handle_auto_turn", "handle_completion_notice", "handle_continuation"]

_ORIGIN = "todo_continuation"
_RETRY_S = 5.0


def _retry() -> EffectResult:
    return EffectResult(retry_after_s=_RETRY_S)


def _outcome_refused(result: object) -> bool:
    """Mirror the continuation gate's contract: anything but ``triggered`` refused."""
    outcome = getattr(result, "outcome", None)
    if outcome is None:
        return False
    return str(getattr(outcome, "value", outcome)).lower() != "triggered"


async def handle_continuation(payload: dict) -> EffectResult:
    """Deliver a queued continuation directive through the auto-turn seam."""
    from runtime import hooks

    trigger = hooks.resolve(hooks.MAYBE_TRIGGER_AUTO_TURN)
    if trigger is None:
        return EffectResult(abandon=True)
    session_key = str(payload.get("session_key") or "")
    prompt = str(payload.get("prompt") or "")
    if not session_key or not prompt:
        return EffectResult(abandon=True)
    injection = HumanMessage(content=prompt, metadata={"origin": _ORIGIN, "internal": True})
    try:
        result = await trigger(session_key, injection)
    except Exception:  # noqa: BLE001 - a transient trigger failure retries
        logger.exception("effects: continuation trigger failed")
        return _retry()
    return _retry() if _outcome_refused(result) else EffectResult(done=True)


async def handle_completion_notice(payload: dict) -> EffectResult:
    """Deliver one subagent completion notice through the existing announce path."""
    run_id = str(payload.get("run_id") or "")
    if not run_id:
        return EffectResult(abandon=True)
    from agent.tools.subagent.registry import memory

    run = memory.get(run_id)
    if run is None:
        # The run was purged; there is nothing left to announce.
        return EffectResult(abandon=True)
    from agent.tools.subagent.announce.delivery import deliver_subagent_announcement

    try:
        result = await deliver_subagent_announcement(run, _from_effects=True)
    except Exception:  # noqa: BLE001 - the existing delivery semantics retry
        logger.exception("effects: completion delivery failed")
        return _retry()
    if result.success:
        return EffectResult(done=True)
    if getattr(result, "terminal", False):
        return EffectResult(abandon=True)
    return _retry()


async def handle_auto_turn(payload: dict) -> EffectResult:
    """Re-run the normal auto-turn trigger (busy/already-pending retries)."""
    session_key = str(payload.get("session_key") or "")
    content = str(payload.get("content") or "")
    if not session_key or not content:
        return EffectResult(abandon=True)
    metadata = payload.get("metadata")
    from server.service.auto_turn import AutoTurnOutcome, maybe_trigger_auto_turn

    injection = HumanMessage(
        content=content,
        metadata=metadata if isinstance(metadata, dict) else None,
    )
    try:
        result = await maybe_trigger_auto_turn(session_key, injection, _from_effects=True)
    except Exception:  # noqa: BLE001 - a transient trigger failure retries
        logger.exception("effects: auto-turn trigger failed")
        return _retry()
    if result.outcome == AutoTurnOutcome.TRIGGERED:
        return EffectResult(done=True)
    return _retry()
