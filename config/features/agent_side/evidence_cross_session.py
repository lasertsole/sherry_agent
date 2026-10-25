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


class EvidenceRegressionGateConfig(TypedDict):
    """Finish-time batch re-verification of a flow's recorded commands."""

    enabled: bool
    reverify_timeout_s: int
    max_reverify_commands: int
    max_output_chars: int
    # "prioritize": replay affected commands first, then the rest.
    # "filter": replay ONLY commands whose step declared a touched file_deps.
    file_deps_mode: str


EVIDENCE_REGRESSION_GATE: EvidenceRegressionGateConfig = {
    "enabled": True,
    # One command timeout; the whole pass is bounded by max_reverify_commands.
    "reverify_timeout_s": 60,
    "max_reverify_commands": 10,
    "max_output_chars": 500,
    # Declarations reorder the batch; "filter" is the opt-in low-cost mode.
    "file_deps_mode": "prioritize",
}
