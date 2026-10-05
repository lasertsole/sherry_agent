"""Per-session file-change snapshots: read the revert state, ask for a revert.

The same service the agent-side ``file_changes_revert`` tool calls, so the
button in the chat and the tool can never drift. Everything runs in a worker
thread (SQLite + blob I/O), and a successful revert pushes the refreshed frame
so every open client sees the chip clear.
"""

from __future__ import annotations

import asyncio


def file_changes_state(session_id: str) -> dict:
    """The revert state of one session (the WS payload, on demand)."""
    from agent.tools.file_tools.snapshot import file_changes_payload

    return file_changes_payload(session_id)


async def revert_session_file_changes(
    session_id: str,
    *,
    paths: list[str] | None = None,
    to_tool_call_id: str = "",
    dry_run: bool = False,
) -> dict:
    """Run the revert core, then announce it and refresh the frame.

    Two follow-ups when a revert applied, both best-effort:

    * the ``<revert>`` conversation notice — the agent must learn that its edits
      were undone (the conversation itself is never cut);
    * the refreshed ``file_changes_updated`` frame, so every open client sees
      the chip clear.
    """
    from agent.tools.file_tools.revert import append_revert_notice, revert_file_changes

    result = await asyncio.to_thread(
        revert_file_changes,
        session_id,
        paths=paths,
        to_tool_call_id=to_tool_call_id,
        dry_run=dry_run,
    )
    if not dry_run and result.get("success"):
        await append_revert_notice(session_id, result.get("reverted_paths") or [])
        from agent.tools.file_tools.snapshot_push import push_file_changes

        await push_file_changes(session_id)
    return result
