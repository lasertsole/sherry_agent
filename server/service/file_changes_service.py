"""Per-session file-change snapshots: read the revert state, ask for a revert.

The same service the agent-side ``file_changes_revert`` tool calls, so the
button in the chat and the tool can never drift. Everything runs in a worker
thread (SQLite + blob I/O), and a successful revert pushes the refreshed frame
so every open client sees the chip clear.
"""

from __future__ import annotations

import asyncio

from loguru import logger


def file_changes_state(session_id: str) -> dict:
    """The revert state of one session (the WS payload, on demand)."""
    from agent.tools.file_tools.snapshot import file_changes_payload

    return file_changes_payload(session_id)


def revert_session_file_changes(
    session_id: str,
    *,
    paths: list[str] | None = None,
    to_tool_call_id: str = "",
    dry_run: bool = False,
) -> dict:
    """Run the revert core and push the refreshed frame when it applied."""
    from agent.tools.file_tools.revert import revert_file_changes

    result = revert_file_changes(
        session_id,
        paths=paths,
        to_tool_call_id=to_tool_call_id,
        dry_run=dry_run,
    )
    if not dry_run and result.get("success"):
        from agent.tools.file_tools.snapshot_push import push_file_changes

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(push_file_changes(session_id))
        else:
            logger.debug("revert: no running loop, skipping the push for {}", session_id)
    return result
