"""Announce pipeline for delivering sub-agent completion results to requesters."""

from .core import run_subagent_announce_flow
from .delivery import deliver_subagent_announcement
from .output import (
    build_child_completion_findings,
    build_compact_announce_stats_line,
)
from .capture import capture_subagent_completion_reply
from .idempotency import build_idempotency_key
from .dispatch import resolve_dispatch_type, AnnounceDispatchType

__all__ = [
    "run_subagent_announce_flow",
    "deliver_subagent_announcement",
    "build_child_completion_findings",
    "build_compact_announce_stats_line",
    "capture_subagent_completion_reply",
    "build_idempotency_key",
]
