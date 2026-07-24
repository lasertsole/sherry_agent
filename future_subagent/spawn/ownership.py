"""Ownership resolution for sub-agents — which session controls state, thread binding, and completion delivery.

When a parent session proxies a spawn request, the completion owner may differ
from the controller session.
"""

from pydantic import BaseModel


class SubagentSpawnOwnership(BaseModel):
    """Resolved ownership triple: controller, thread-binding requester, and completion delivery target."""
    controller_session_key: str
    thread_binding_requester_session_key: str
    completion_requester_session_key: str
    completion_requester_display_key: str


def _resolve_internal_key(session_key: str | None, main_key: str, alias: str) -> str:
    """Return the canonical alias when the key is empty, otherwise the trimmed key."""
    if not session_key or not session_key.strip():
        return alias
    return session_key.strip()


def _resolve_display_key(session_key: str, main_key: str, alias: str) -> str:
    """Return the alias for display when the key matches it, otherwise the raw key."""
    if session_key == alias:
        return alias
    return session_key


def resolve_spawn_ownership(
    requester_session_key: str,
    completion_owner_key: str | None = None,
    main_key: str = "agent:main:session:default",
    alias: str = "agent:main:session:default",
) -> SubagentSpawnOwnership:
    """Resolve ownership for a sub-agent: controller, thread-binding, and completion delivery keys.

    When requester_session_key contains the main agent identifier without an
    explicit session segment, normalizes it to the canonical alias. Supports
    separate completion_owner_key for proxied spawn scenarios.
    """
    canonical_alias = _resolve_canonical_alias(requester_session_key, main_key, alias)

    effective_requester = requester_session_key.strip() if requester_session_key else canonical_alias
    if ":main:" in effective_requester and ":session:" not in effective_requester:
        # Bare main-agent key without a session segment — normalize to alias
        effective_requester = canonical_alias

    controller_session_key = effective_requester

    trimmed_owner = completion_owner_key.strip() if completion_owner_key else None
    completion_requester_session_key = (
        _resolve_internal_key(trimmed_owner, main_key, canonical_alias)
        if trimmed_owner
        else controller_session_key
    )

    completion_requester_display_key = _resolve_display_key(
        completion_requester_session_key, main_key, canonical_alias
    )

    return SubagentSpawnOwnership(
        controller_session_key=controller_session_key,
        thread_binding_requester_session_key=controller_session_key,
        completion_requester_session_key=completion_requester_session_key,
        completion_requester_display_key=completion_requester_display_key,
    )


def _resolve_canonical_alias(session_key: str, main_key: str, default_alias: str) -> str:
    """Extract the canonical 'agent:main:session:<id>' alias from a main-agent session key."""
    if ":main:" in session_key:
        parts = session_key.split(":")
        if len(parts) >= 4:
            return f"agent:main:session:{parts[-1]}"  # rebuild with the trailing session identifier
    return default_alias
