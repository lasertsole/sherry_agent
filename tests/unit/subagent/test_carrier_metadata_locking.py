"""Locking (characterization) tests: BaseMessage.metadata survives critical paths.

Wave-2 safety net for plan subagent-origin-tagging (Task 2). These tests are
GREEN on the untouched codebase — they lock CURRENT behavior; they do not
create it:

1. test_sanitize_preserves_carrier_metadata — the frozen-contract carrier
   HumanMessage (completion_message.py:57-89) passes through
   sanitize_tool_use_result_pairing BY REFERENCE (transcript_repair.py:202
   ``out.append(msg)`` for non-AIMessage input), keeping all four metadata keys.
2. test_rehydrated_carrier_keeps_provenance — after enqueue + "restart" (a NEW
   SteeringQueue instance over the same SQLite db), _rebuild_message
   (steering_queue.py:95-109) restores metadata internal/provenance/run_id
   (without 'status' — the documented PendingInjection API gap).
"""

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.tools.subagent.announce.completion_message import (
    PROVENANCE,
    build_completion_message,
)
from agent.tools.subagent.announce.steering_queue import SteeringQueue
from agent.tools.subagent.registry.pending_injections import PendingInjectionStore
from agent.tools.subagent.types.registry import SubagentRunRecord
from pub_func.transcript_repair import sanitize_tool_use_result_pairing

pytestmark = pytest.mark.unit


def _make_run(
    run_id: str = "run-abc-123",
    label: str | None = "test-child",
    task_name: str | None = "research",
) -> SubagentRunRecord:
    return SubagentRunRecord(
        run_id=run_id,
        child_session_key="agent:main:subagent:uuid-1",
        requester_session_key="sess-1",
        task="do research",
        label=label,
        task_name=task_name,
    )


# ---------------------------------------------------------------------------
# Path 1: transcript sanitize (pub_func/transcript_repair.py:158-229)
# ---------------------------------------------------------------------------


def test_sanitize_preserves_carrier_metadata():
    """Carrier HumanMessage survives sanitize_tool_use_result_pairing intact.

    Locks the by-reference passthrough at transcript_repair.py:202: a
    non-AIMessage is appended to the output unchanged, so the frozen metadata
    contract {internal, provenance, run_id, status} cannot be stripped.
    """
    carrier = build_completion_message(
        _make_run(run_id="run-san-1"), "child finished", "completed"
    )
    carrier_meta = getattr(carrier, "metadata", None) or {}
    assert carrier_meta.get("provenance") == PROVENANCE  # sanity: frozen contract

    ai = AIMessage(
        content="",
        tool_calls=[
            {"name": "search", "args": {"q": "x"}, "id": "call-1", "type": "tool_call"}
        ],
    )
    tool = ToolMessage(content="result", tool_call_id="call-1")

    repaired = sanitize_tool_use_result_pairing([carrier, ai, tool])

    # Passed through BY REFERENCE (line 202: out.append(msg)) — no copy, no rewrite.
    assert repaired[0] is carrier
    assert isinstance(repaired[0], HumanMessage)

    meta = getattr(repaired[0], "metadata", None) or {}
    assert set(meta.keys()) == {"internal", "provenance", "run_id", "status"}
    assert meta == {
        "internal": True,
        "provenance": "subagent_completion",
        "run_id": "run-san-1",
        "status": "completed",
    }


# ---------------------------------------------------------------------------
# Path 2: steering queue rehydration (announce/steering_queue.py:95-109)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rehydrated_carrier_keeps_provenance(tmp_path: Path):
    """A NEW SteeringQueue over the same SQLite db rebuilds carrier metadata.

    Locks _rebuild_message (steering_queue.py:95-109): the rehydrated message
    carries metadata provenance == 'subagent_completion' and internal is True,
    so the task 7 skip check keeps working after a crash/restart. ('status' is
    intentionally absent on rebuilds — documented PendingInjection API gap.)
    """
    db_file = tmp_path / "rehydrate.db"
    queue_a = SteeringQueue(store=PendingInjectionStore(db_path=db_file))
    carrier = build_completion_message(
        _make_run(run_id="run-re-1"), "pre-crash result", "completed"
    )
    _ = await queue_a.enqueue_steering("sess-re", carrier)

    # "Restart": brand-new queue instance over the SAME storage.
    queue_b = SteeringQueue(store=PendingInjectionStore(db_path=db_file))
    hydrated = await queue_b.rehydrate("sess-re")
    assert [item.run_id for item in hydrated] == ["run-re-1"]

    rebuilt = hydrated[0].message
    assert isinstance(rebuilt, HumanMessage)
    meta = getattr(rebuilt, "metadata", None) or {}
    assert meta["provenance"] == "subagent_completion"
    assert meta["internal"] is True
    assert meta["run_id"] == "run-re-1"
    # Content-identical rebuild: full original marker text is preserved.
    assert rebuilt.content == carrier.content
