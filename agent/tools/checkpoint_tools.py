"""Agent-facing shadow checkpoint tool (list / show / restore)."""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from agent.tools.pub_base import SessionId
from agent.tools.pub_base import shadow_git


class CheckpointArgs(BaseModel):
    action: Literal["list", "show", "restore"] = Field(
        description="list checkpoints, show one, or restore files from one"
    )
    commit_hash: str = Field(
        default="", description="checkpoint hash from action=list (show/restore)"
    )
    file_path: str = Field(
        default="", description="optional path for show (diff) or restore (one file)"
    )
    force: bool = Field(
        default=False,
        description="restore: accepted for compatibility; a pre-restore snapshot is always taken",
    )
    limit: int = Field(default=10, description="list: number of checkpoints (max 50)")


@tool("checkpoint", args_schema=CheckpointArgs)
async def checkpoint_tool(
    action: Literal["list", "show", "restore"],
    commit_hash: str = "",
    file_path: str = "",
    force: bool = False,
    limit: int = 10,
    session_id: SessionId = "",
) -> str:
    """List, inspect, or restore shadow-git checkpoints.

    Snapshots are taken automatically before the first file/terminal change of
    each turn. Use list to find a checkpoint, show to inspect it, and restore to
    put files back (a pre-restore snapshot is taken first, so the undo can be
    undone).
    """
    sid = session_id.strip()
    if not sid:
        return "Error: session_id is required"
    try:
        if action == "list":
            items = shadow_git.list_checkpoints(sid, limit=max(1, min(int(limit), 50)))
            return json.dumps(items, ensure_ascii=False)
        if action == "show":
            target = commit_hash.strip()
            if not target:
                return "Error: commit_hash is required for show"
            if file_path.strip():
                return json.dumps(
                    {
                        "hash": target,
                        "file": file_path.strip(),
                        "diff": shadow_git.read_commit_diff(sid, target, file_path.strip()),
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "hash": target,
                    "changed_files": shadow_git.read_commit_files(sid, target),
                },
                ensure_ascii=False,
            )
        if action == "restore":
            target = commit_hash.strip()
            if not target:
                items = shadow_git.list_checkpoints(sid, limit=1)
                if not items:
                    return "Error: no checkpoints to restore"
                target = items[0]["hash"]
            result = await asyncio.to_thread(
                shadow_git.restore_checkpoint,
                sid,
                target,
                file_path=file_path.strip() or None,
            )
            return json.dumps({**result, "forced": bool(force)}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - tool boundary
        return f"Error: {type(exc).__name__}: {exc}"
    return f"Error: unknown action {action!r}"


def build_checkpoint_tool():
    checkpoint_tool.metadata = {
        "scope": "main_only",
        "idempotent": False,
        # list/show only read the shadow repository; restore writes files and
        # must never be treated as a repeatable call by ToolGuardrails.
        "idempotent_actions": ("list", "show"),
        "concurrent_safe": False,
        "side_effect_scope": "workspace",
    }
    return checkpoint_tool


__all__ = ["build_checkpoint_tool"]
