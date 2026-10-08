"""Shared, monkeypatchable dispatch seam for the taskflow tool family.

The subagent runtime is imported lazily inside ``dispatch_child`` so this
module stays importable without the spawn pipeline (the family's standalone
load contract). Tools MUST call it module-qualified (``_dispatch.dispatch_child``)
so tests can substitute the seam without touching the real pipeline.
"""


async def dispatch_child(
    task: str,
    requester_session_key: str,
    label: str | None = None,
    functional_role: str | None = None,
    output_schema: dict | None = None,
    step_model: str | None = None,
    step_timeout_seconds: float | None = None,
) -> str:
    """Dispatch a detached child session and return its child_session_key.

    ``functional_role`` optionally selects a functional specialization
    (general / researcher / executor / reviewer / librarian); ``None`` keeps the
    default spawn path. ``output_schema`` is the step's expectation contract: it
    is handed to the child as a structured-output requirement (validated again on
    resume, Tier 1). ``step_model`` overrides the child model and
    ``step_timeout_seconds`` the child run timeout; both ``None`` = spawn defaults.

    Raises RuntimeError unless the spawn is accepted with a child key.
    """
    from agent.tools.subagent import spawn_subagent_direct

    result = await spawn_subagent_direct(
        task=task,
        requester_session_key=requester_session_key,
        label=label,
        expects_completion_message=True,
        functional_role_hint=functional_role,
        output_schema=output_schema,
        model=step_model,
        run_timeout_seconds=step_timeout_seconds,
    )
    if result.status != "accepted" or not result.child_session_key:
        raise RuntimeError(f"status={result.status} error={result.error}")
    return result.child_session_key
