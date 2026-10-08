"""Guidance text returned to the caller after a successful spawn.

Tells the caller not to poll — results are push-delivered.
"""


def resolve_spawn_accepted_note(
    agent_session_key: str | None = None,
) -> str:
    """Return the acceptance note text for a spawned sub-agent."""
    if agent_session_key:
        return (
            "Subagent spawned in run mode. DO NOT poll for results — "
            "the result will be delivered to you automatically when complete. "
            "Use sessions_yield() to wait for completion."
        )

    return (
        "Subagent spawned in run mode. DO NOT poll for results — "
        "the result will be delivered to you automatically when complete."
    )
