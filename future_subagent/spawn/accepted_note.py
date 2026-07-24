"""Guidance text returned to the caller after a successful spawn.

RUN mode: tells the caller not to poll — results are push-delivered.
SESSION mode: explains thread-binding and follow-up messaging.
"""

from ..types.spawn import SpawnMode


def resolve_spawn_accepted_note(
    spawn_mode: SpawnMode,
    agent_session_key: str | None = None,
) -> str:
    """Return the acceptance note text appropriate for the spawn mode."""
    if spawn_mode == SpawnMode.SESSION:
        return (
            "Subagent session created and bound to thread. "
            "Use sessions_send(sessionKey=...) to send follow-up messages, "
            "or sessions_yield() to wait for the subagent to finish."
        )

    if agent_session_key:
        return (
            f"Subagent spawned in run mode. DO NOT poll for results — "
            f"the result will be delivered to you automatically when complete. "
            f"Use sessions_yield() to wait for completion."
        )

    return (
        "Subagent spawned in run mode. DO NOT poll for results — "
        "the result will be delivered to you automatically when complete."
    )
