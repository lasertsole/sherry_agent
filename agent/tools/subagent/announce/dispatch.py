"""Multi-stage announce dispatch scheduler.

Implements a strategy-based dispatch (currently direct-primary only, extensible
to steer-primary → direct-primary → steer-fallback).
"""

from enum import Enum
from loguru import logger
from ..types.registry import SubagentRunRecord


class AnnounceDispatchType(str, Enum):
    """Enumeration of dispatch strategy types."""

    DIRECT = "direct"
    STEER = "steer"


class AnnounceDeliveryResult:
    """Result of an announce delivery attempt, including dispatch path tracing."""

    def __init__(
        self,
        success: bool,
        error: str | None = None,
        suspended: bool = False,
        dispatch_path: list[str] | None = None,
        terminal: bool = False,
    ):
        """Initialize delivery result with success status, optional error, and dispatch tracing info."""
        self.success = success
        self.error = error
        self.suspended = suspended
        self.dispatch_path = dispatch_path or []
        self.terminal = terminal

    def with_path(self, path: str) -> "AnnounceDeliveryResult":
        """Append a dispatch strategy name to the path for tracing."""
        self.dispatch_path.append(path)
        return self


def resolve_dispatch_type(run: SubagentRunRecord) -> AnnounceDispatchType:
    """Determine the dispatch type for a given run (currently always DIRECT)."""
    return AnnounceDispatchType.DIRECT


async def run_announce_dispatch(
    run: SubagentRunRecord,
    deliver_fn,
) -> AnnounceDeliveryResult:
    """Try each dispatch strategy in order until one succeeds or all fail."""
    strategies = _build_dispatch_strategies(run)

    for strategy_name, strategy_fn in strategies:
        try:
            result = await strategy_fn(run, deliver_fn)
            result.with_path(strategy_name)
            if result.success or result.suspended:
                return result
        except Exception as e:
            logger.warning(
                "Dispatch strategy {} failed for run {}: {}",
                strategy_name, run.run_id, e,
            )
            continue

    return AnnounceDeliveryResult(
        success=False,
        error="All dispatch strategies failed",
        dispatch_path=[s[0] for s in strategies],
        terminal=True,
    )


def _build_dispatch_strategies(run: SubagentRunRecord) -> list[tuple[str, callable]]:
    """Build the ordered list of dispatch strategy tuples (name, fn)."""
    strategies = [
        ("direct-primary", _dispatch_direct),
    ]
    return strategies


async def _dispatch_direct(
    run: SubagentRunRecord,
    deliver_fn,
) -> AnnounceDeliveryResult:
    """Direct dispatch: invoke the delivery function without intermediate steps."""
    return await deliver_fn(run)
