"""Links from superagent notices into the Sherry UI."""

from __future__ import annotations

from urllib.parse import quote


def session_url(base_url: str, session_key: str) -> str:
    """Return a Sherry UI link for a Session."""
    return f"{base_url.rstrip('/')}/#/chat/{quote(session_key, safe='')}"


def ticket_url(base_url: str, ticket_id: str) -> str:
    """Return a Sherry UI link for a Ticket's panel view."""
    return f"{base_url.rstrip('/')}/#/superagent/tickets/{quote(ticket_id, safe='')}"


__all__ = ["session_url", "ticket_url"]
