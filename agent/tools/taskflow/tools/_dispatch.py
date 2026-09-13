"""Shared, monkeypatchable dispatch seam for the taskflow tool family.

The subagent runtime is imported lazily inside ``dispatch_child`` so this
module stays importable without the spawn pipeline (the family's standalone
load contract). Tools MUST call it module-qualified (``_dispatch.dispatch_child``)
so tests can substitute the seam without touching the real pipeline.
"""


async def dispatch_child(task: str, requester_session_key: str, label: str | None = None) -> str:
    """Dispatch a detached child session and return its child_session_key.

    Raises RuntimeError unless the spawn is accepted with a child key.
    """
    from agent.tools.subagent import spawn_subagent_direct

    result = await spawn_subagent_direct(
        task=task,
        requester_session_key=requester_session_key,
        label=label,
        expects_completion_message=True,
    )
    if result.status != "accepted" or not result.child_session_key:
        raise RuntimeError(f"status={result.status} error={result.error}")
    return result.child_session_key
