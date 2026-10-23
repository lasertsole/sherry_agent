"""Cross-session evidence staleness: propagate a write to every session it may invalidate.

A file edit currently marks only the writer's own session stale. When a
subagent overwrites a file another session verified, that other session's
evidence would keep reporting ``pass``. This switch appends a stale event to
every OTHER session whose evidence command names the edited path, so the
existing collectors and finish gates see the invalidation without changing the
ledger schema.
"""

from typing import TypedDict


class EvidenceCrossSessionConfig(TypedDict):
    """The propagation switch, scan cap and self-session policy."""

    enabled: bool
    max_sessions_scan: int
    exclude_self: bool


EVIDENCE_CROSS_SESSION_STALENESS: EvidenceCrossSessionConfig = {
    "enabled": True,
    # A cap keeps one write from scanning an unbounded number of sessions.
    "max_sessions_scan": 200,
    # The writer's own session is already covered by the per-session mark.
    "exclude_self": True,
}
