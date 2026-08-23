"""Resolve the announce origin and delivery target routing.

Detects whether the requester is also a sub-agent session to choose between
internal injection vs user completion message delivery path.
"""

from ..types.registry import SubagentRunRecord
from ..registry.read import get_run_by_child_session_key_readonly


class AnnounceOrigin:
    """Describes the origin and routing context for a sub-agent announcement."""

    def __init__(
        self,
        child_session_key: str,
        requester_session_key: str,
        controller_session_key: str | None,
        run_id: str,
        agent_id: str,
        depth: int,
        dispatch_target: str,
        is_requester_subagent: bool,
        requester_run: SubagentRunRecord | None,
    ):
        self.child_session_key = child_session_key
        self.requester_session_key = requester_session_key
        self.controller_session_key = controller_session_key
        self.run_id = run_id
        self.agent_id = agent_id
        self.depth = depth
        self.dispatch_target = dispatch_target
        self.is_requester_subagent = is_requester_subagent
        self.requester_run = requester_run


def resolve_announce_origin(run: SubagentRunRecord) -> AnnounceOrigin:
    """Resolve where the announcement should be routed based on requester hierarchy."""
    target = run.requester_session_key
    if run.controller_session_key and run.controller_session_key != run.requester_session_key:
        # Prefer the controller over the requester when they differ
        target = run.controller_session_key

    requester_run = _find_requester_run(run.requester_session_key)
    is_requester_subagent = requester_run is not None

    if is_requester_subagent and requester_run.controller_session_key:
        # Route to the requester's controller when the requester is itself a sub-agent,
        # so the announcement reaches the top-level orchestrator rather than an intermediate node
        target = requester_run.controller_session_key

    return AnnounceOrigin(
        child_session_key=run.child_session_key,
        requester_session_key=run.requester_session_key,
        controller_session_key=run.controller_session_key,
        run_id=run.run_id,
        agent_id=run.agent_id,
        depth=run.depth,
        dispatch_target=target,
        is_requester_subagent=is_requester_subagent,
        requester_run=requester_run,
    )


def _find_requester_run(requester_session_key: str) -> SubagentRunRecord | None:
    """Look up the run record for a requester session key, if it is a sub-agent."""
    return get_run_by_child_session_key_readonly(requester_session_key)
