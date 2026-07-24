"""Requester-origin routing — resolves channel, account, and thread context for a child session."""

from pydantic import BaseModel


class ChildSessionOrigin(BaseModel):
    """Origin metadata extracted from the requester's session key: channel, account, thread, and group space."""
    channel: str | None = None
    account_id: str | None = None
    thread_id: str | None = None
    group_space: str | None = None
    member_role_ids: list[str] = []


def resolve_requester_origin_for_child(
    requester_session_key: str,
    agent_id: str = "main",
) -> ChildSessionOrigin:
    """Parse the requester's session key to derive origin metadata for the child session."""
    parts = requester_session_key.split(":")

    origin = ChildSessionOrigin()

    if len(parts) >= 4:
        origin.account_id = parts[2] if len(parts) > 2 else None
        origin.channel = parts[0] if parts[0] else None

    return origin


def build_origin_fingerprint(origin: ChildSessionOrigin) -> str:
    """Build a pipe-delimited fingerprint string from origin fields for logging/comparison."""
    parts = []
    if origin.channel:
        parts.append(f"ch:{origin.channel}")
    if origin.account_id:
        parts.append(f"acc:{origin.account_id}")
    if origin.thread_id:
        parts.append(f"tid:{origin.thread_id}")
    if origin.group_space:
        parts.append(f"gs:{origin.group_space}")
    return "|".join(parts)
