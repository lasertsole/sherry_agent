"""Quiesce an eval session before the harness touches the instance worktree.

A finished turn only means the MAIN graph returned. Detached subagents, a
background terminal, or a tool thread cancelled mid-flight can still write the
instance repository after that point. The harness then diffs, applies the test
patch, runs pytest and reverts — all on the same tree — so every late writer is
a potential corrupted verdict or a failed ``git clean`` (the observed
``.sherry`` EPERM).

``quiesce_session`` stops those writers with explicit APIs instead of timing
retries, and freezes shadow-git snapshots until the harness is done with the
repo. It is bounded and fail-open: a timeout is reported to the caller, never
raised, so one wedged instance cannot abort the whole run.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from loguru import logger

__all__ = ["quiesce_session"]


async def _wait_until(predicate: Any, timeout: float, *, interval: float = 0.05) -> bool:
    """Poll ``predicate`` until true or ``timeout``; exceptions read as false."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - a broken probe must not abort quiesce
            logger.debug("quiesce probe failed", exc_info=True)
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(interval)


async def quiesce_session(
    session_id: str,
    root: Path,
    *,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Stop every writer of one eval session and freeze its shadow snapshots.

    Returns ``{"ok", "timed_out", "active_descendants", "background_running",
    "shadow_writers"}``. ``timed_out`` means the harness must mark the instance
    as a harness error rather than judging a possibly inconsistent tree.
    """
    deadline = time.monotonic() + max(0.0, timeout_s)
    result: dict[str, Any] = {
        "ok": False,
        "timed_out": False,
        "active_descendants": None,
        "background_running": None,
        "shadow_writers": None,
    }

    # 1) The main turn (a no-op when it already finished). Bounded internally.
    try:
        from server.service.turn_runner import cancel_session_turn

        await cancel_session_turn(session_id)
    except Exception:  # noqa: BLE001 - teardown must keep going
        logger.warning("quiesce: cancel_session_turn failed for {}", session_id, exc_info=True)

    requester = ""
    try:
        from agent.tools.subagent.registry.session_keys import denormalize_session_key

        requester = denormalize_session_key(session_id)
    except Exception:  # noqa: BLE001
        logger.debug("quiesce: session key normalization failed", exc_info=True)

    # 2) Detached subagents: cascade-kill the whole descendant tree, then wait
    #    for the registry to report no RUNNING/PENDING runs.
    if requester:
        try:
            from agent.tools.subagent.control.kill import (
                kill_subagent_run_with_cascade,
                list_killable_children,
            )

            for child in list_killable_children(requester):
                await kill_subagent_run_with_cascade(
                    child.run_id,
                    reason="eval_teardown",
                    cascade=True,
                    requester_session_key=requester,
                )
        except Exception:  # noqa: BLE001
            logger.warning("quiesce: descendant kill failed for {}", requester, exc_info=True)

        try:
            from agent.tools.subagent.registry.queries import (
                count_active_descendant_runs,
            )

            budget = min(10.0, max(0.0, deadline - time.monotonic()))
            await _wait_until(lambda: count_active_descendant_runs(requester) == 0, budget)
            result["active_descendants"] = count_active_descendant_runs(requester)
        except Exception:  # noqa: BLE001
            logger.debug("quiesce: descendant wait failed", exc_info=True)

    # 3) Background terminals: kill the process tree, then wait for the ledger
    #    to report no running tasks.
    try:
        from agent.tools.terminal.bg_registry import background_registry

        background_registry.kill_session_tasks(session_id)
        budget = min(5.0, max(0.0, deadline - time.monotonic()))
        await _wait_until(lambda: background_registry.running_count(session_id) == 0, budget)
        result["background_running"] = background_registry.running_count(session_id)
    except Exception:  # noqa: BLE001
        logger.warning("quiesce: background-terminal teardown failed", exc_info=True)

    # 4) Shadow-git: wait for any in-flight snapshot/restore, then freeze so a
    #    late task cannot recreate the store while the harness reverts.
    try:
        from agent.tools.pub_base import shadow_git

        budget = min(5.0, max(0.0, deadline - time.monotonic()))
        shadow_git.wait_for_idle(root, timeout=budget)
        shadow_git.freeze(root)
        shadow_git.wait_for_idle(root, timeout=min(5.0, max(0.0, deadline - time.monotonic())))
        result["shadow_writers"] = shadow_git.active_writers(root)
    except Exception:  # noqa: BLE001
        logger.warning("quiesce: shadow-git teardown failed", exc_info=True)

    result["ok"] = (
        result["active_descendants"] in (None, 0)
        and result["background_running"] in (None, 0)
        and result["shadow_writers"] in (None, 0)
        and time.monotonic() <= deadline
    )
    result["timed_out"] = not result["ok"]
    if not result["ok"]:
        logger.warning("quiesce: session {} not fully idle ({})", session_id, result)
    return result
