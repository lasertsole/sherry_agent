"""Pull Request following (prototype ``src/sources/github-pull.ts``).

A Ticket follows every PR it opened (``own``) plus the PR it originated from
(observed).  It ends when all of its followed PRs are closed, unless a person
ended it or a retry/recovery is still in flight.
"""

from __future__ import annotations

from typing import Any

from superagent.core.model import Follow, Receipt, SourceRef, Ticket

CLOSED_STATES = frozenset({"closed", "merged"})


def pull_thread(repo: str, number: str | int) -> SourceRef:
    """Return the Thread reference for a Pull Request."""
    return SourceRef(source="github", id=f"{repo}#pull-{number}")


def claim_pull(ticket: Ticket, number: str | int, head: str = "") -> Follow:
    """Build the follow for a PR this Ticket opened."""
    return Follow(thread=pull_thread(_repo(ticket), number), own=True, closed={"head": head})


def observe_pull(ticket: Ticket, number: str | int) -> Follow:
    """Build the follow for the PR that originated this Ticket."""
    return Follow(thread=pull_thread(_repo(ticket), number), own=False)


def _repo(ticket: Ticket) -> str:
    for channel in ticket.channels:
        if channel.source == "github":
            return str(channel.address.get("repo", ""))
    return str(ticket.origin.get("repo", ""))


def _number(follow: Follow) -> str:
    return follow.thread.id.rsplit("#pull-", 1)[-1]


def has_follow(ticket: Ticket, number: str | int) -> bool:
    """Return whether a Ticket already follows a PR."""
    target = str(number)
    return any(_number(follow) == target for follow in ticket.follows)


def reopen(ticket: Ticket, number: str | int) -> bool:
    """Clear the closed marker of a reopened PR."""
    for follow in ticket.follows:
        if _number(follow) == str(number):
            follow.closed = None
            return True
    return False


def all_follows_closed(ticket: Ticket, closed_numbers: set[str]) -> bool:
    """Return whether every followed PR is closed."""
    return bool(ticket.follows) and all(
        _number(follow) in closed_numbers for follow in ticket.follows
    )


def end_notice(ticket: Ticket, outcome: str, at: str) -> Receipt | None:
    """Build a stable chat notice when a followed PR ends."""
    if not ticket.channels:
        return None
    from superagent.core.ids import key
    from superagent.core.model import Channel

    return Receipt(
        id=key("github", "notice", ticket.id, outcome),
        ticket_id=ticket.id,
        channel=ticket.channels[0],
        purpose=Channel.RESULT,
        text=f"Ticket {ticket.id} ended: {outcome}.",
    )


def apply_pull_event(
    ticket: Ticket, event: dict[str, Any], *, reopened: bool = False
) -> tuple[bool, bool]:
    """Apply a PR state event; return ``(changed, should_end)``."""
    number = str(event.get("number", ""))
    if not number:
        return False, False
    if not has_follow(ticket, number):
        follow = observe_pull(ticket, number)
        ticket.follows.append(follow)
        changed = True
    else:
        changed = False
    state = str(event.get("state", "")).lower()
    if state == "reopened" or reopened:
        changed = reopen(ticket, number) or changed
        return changed, False
    if state in CLOSED_STATES:
        for follow in ticket.follows:
            if _number(follow) == number:
                follow.closed = {"state": state}
        closed = {_number(follow) for follow in ticket.follows if follow.closed is not None}
        return True, all_follows_closed(ticket, closed)
    return changed, False


__all__ = [
    "CLOSED_STATES",
    "all_follows_closed",
    "apply_pull_event",
    "claim_pull",
    "end_notice",
    "observe_pull",
    "pull_thread",
]
