"""Least-privilege scope resolution — assigns minimal permission scopes based on the sub-agent's role."""

from pydantic import BaseModel
from ..types.capability import SubagentSessionRole


class SubagentLaunchAuthorization(BaseModel):
    """Authorization record tying a caller session to a target agent with specific scopes."""
    caller_session_key: str
    target_agent_id: str
    scopes: list[str] = []


def resolve_least_privilege_scopes(
    agent_id: str,
    role: SubagentSessionRole,
) -> list[str]:
    """Return the minimal set of permission scopes for a sub-agent based on its role.

    All roles get subagent:read; orchestrators additionally get spawn/kill/yield/send.
    """
    scopes = ["subagent:read"]

    if role == SubagentSessionRole.ORCHESTRATOR:
        scopes.extend([
            "subagent:spawn",
            "subagent:kill",
            "subagent:yield",
            "subagent:send",
        ])
    elif role == SubagentSessionRole.LEAF:
        scopes.extend([
            "subagent:yield",
        ])

    return scopes
