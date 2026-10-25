"""compact_now — user-requested context compaction (ZCode ``UserRequested``).

The main agent calls this when the operator explicitly asks to compact the
conversation. It only QUEUES the request: the next model call's T2 dispatch sees
the flag and runs one compaction even below the automatic thresholds, bypassing
the anti-thrash gate the same way T4/T5 forced recovery does. The tool returns
immediately, so the model can tell the user what will happen.
"""

from __future__ import annotations

import json

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from agent.tools.pub_base import SessionId
from runtime import state_register_mem
from runtime.session.state_keys import StateKey


class CompactNowInput(BaseModel):
    """Schema for the ``compact_now`` tool arguments."""

    session_id: SessionId = ""
    reason: str = Field(
        default="",
        description="Short reason supplied by the user (optional, for the log).",
    )


class CompactNowTool(BaseTool):
    """Queue one user-requested compaction for the next model call."""

    name: str = "compact_now"
    description: str = (
        "Compact the conversation now because the USER explicitly asked to. Use only when "
        "the user requests compaction (for example they typed /compact or asked to shrink "
        "the context); never call it on your own initiative. It queues one compaction for "
        "the next model call and returns immediately."
    )
    args_schema: type[BaseModel] = CompactNowInput
    metadata: dict = {"idempotent": False, "side_effect_scope": "session"}

    def _run(self, session_id: str = "", reason: str = "") -> str:
        session_id = (session_id or "").strip()
        if not session_id:
            return json.dumps(
                {"ok": False, "error": "no session id; compaction was not queued"},
                ensure_ascii=False,
            )
        state_register_mem.set_state(session_id, StateKey.SUMMARIZATION_COMPACT_REQUESTED, True)
        # Bypass the anti-thrash gate for this one request (T4/T5 semantics).
        state_register_mem.set_state(session_id, StateKey.SUMMARIZATION_FORCE_RECOVERY, True)
        return json.dumps(
            {
                "ok": True,
                "status": "queued",
                "reason": (reason or "").strip(),
                "message": "Compaction will run before the next model call.",
            },
            ensure_ascii=False,
        )


def build_compact_now_tool() -> BaseTool:
    """Factory used by the main tool registry."""
    return CompactNowTool()
