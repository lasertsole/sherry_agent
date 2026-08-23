"""Target policy validation — controls which agent types are allowed to be spawned."""

from ..config import get_config


def resolve_allowed_target_ids(agent_id: str | None = None) -> list[str]:
    """Return the list of agent IDs allowed as spawn targets from config."""
    config = get_config()
    return config.allow_agents


def is_target_allowed(target_agent_id: str, allow_agents: list[str] | None = None) -> tuple[bool, str]:
    """Check whether a target agent ID is in the allow-list; '*' permits all."""
    if allow_agents is None:
        allow_agents = resolve_allowed_target_ids()

    # Wildcard: allow all targets
    if "*" in allow_agents:
        return True, ""
    if target_agent_id in allow_agents:
        return True, ""

    return False, f"Agent '{target_agent_id}' not in allowed list: {allow_agents}"


def validate_target_policy(
    target_agent_id: str,
    requester_agent_id: str,
) -> tuple[bool, str]:
    """Validate that the target agent is allowed to be spawned by the requester."""
    allowed, reason = is_target_allowed(target_agent_id)
    if not allowed:
        return False, reason

    # Self-spawn is always allowed (an agent may create its own type)
    if target_agent_id == requester_agent_id:
        return True, ""

    return True, ""
