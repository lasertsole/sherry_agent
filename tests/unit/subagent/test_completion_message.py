"""Unit tests for the synthetic completion message builder (task 4, Wave 1).

Covers the MUST-DO behaviors from the task spec:
1. One test per status (completed/failed/interrupted): exact text format,
   exact frozen metadata contract, human role
2. Content passthrough (marker line + newline + content, multiline preserved)
3. Strict status validation (registry enums like 'ok'/'error' are rejected —
   the caller maps RunOutcomeStatus to the plan vocabulary)
4. Name resolution: child_name (Task 5 PendingInjection) → label → task_name
5. Task 7 skip-contract: a ≤5-line provenance check classifies the message
"""

import pytest
from langchain_core.messages import HumanMessage

from agent.tools.subagent.announce.completion_message import (
    build_completion_message,
)
from agent.tools.subagent.types.registry import SubagentRunRecord

_EXPECTED_METADATA_KEYS = {"internal", "provenance", "run_id", "status"}


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


# --------------------------------------------------------------------------
# Per-status text format + frozen metadata + human role
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
def test_status_message_text_metadata_and_role(status: str):
    msg = build_completion_message(_make_run(), "all done", status)

    assert isinstance(msg, HumanMessage)
    assert msg.type == "human"
    assert msg.content.startswith(f"[subagent:test-child {status}]")
    assert msg.content.endswith("all done")
    assert msg.metadata == {
        "internal": True,
        "provenance": "subagent_completion",
        "run_id": "run-abc-123",
        "status": status,
    }


# --------------------------------------------------------------------------
# Content passthrough
# --------------------------------------------------------------------------


def test_content_passthrough_multiline():
    content = "line1\nline2\n\nline4"
    msg = build_completion_message(_make_run(), content, "completed")
    assert msg.content == "[subagent:test-child completed]\n" + content


def test_empty_content_marker_only():
    msg = build_completion_message(_make_run(), "", "failed")
    assert msg.content == "[subagent:test-child failed]"
    assert msg.metadata["status"] == "failed"


def test_none_content_marker_only():
    msg = build_completion_message(_make_run(), None, "interrupted")
    assert msg.content == "[subagent:test-child interrupted]"


# --------------------------------------------------------------------------
# Metadata contract exactness
# --------------------------------------------------------------------------


def test_metadata_contract_exact_keys():
    msg = build_completion_message(_make_run(run_id="run-xyz"), "payload", "completed")
    assert set(msg.metadata.keys()) == _EXPECTED_METADATA_KEYS
    assert msg.metadata["internal"] is True
    assert msg.additional_kwargs == {}  # metadata must not leak into additional_kwargs


# --------------------------------------------------------------------------
# Status validation (pure function: no auto-detection, fail fast)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad_status", ["ok", "error", "timeout", "killed", "", "COMPLETED"])
def test_invalid_status_rejected(bad_status: str):
    with pytest.raises(ValueError):
        build_completion_message(_make_run(), "payload", bad_status)


# --------------------------------------------------------------------------
# Name resolution: child_name → label → task_name → "unknown"
# --------------------------------------------------------------------------


def test_name_prefers_label_over_task_name():
    msg = build_completion_message(_make_run(label="lucky", task_name="research"), "p", "completed")
    assert msg.content.startswith("[subagent:lucky ")


def test_name_falls_back_to_task_name():
    msg = build_completion_message(_make_run(label=None, task_name="research"), "p", "completed")
    assert msg.content.startswith("[subagent:research ")


def test_name_accepts_task5_pending_injection_shape():
    """Task 5's PendingInjection carries child_name — the builder must accept it duck-typed."""
    from agent.tools.subagent.registry.pending_injections import PendingInjection

    injection = PendingInjection(
        run_id="run-pi-1",
        requester_session_key="sess-1",
        child_name="scout",
        content="found it",
    )
    msg = build_completion_message(injection, "found it", "completed")
    assert msg.content == "[subagent:scout completed]\nfound it"
    assert msg.metadata["run_id"] == "run-pi-1"


# --------------------------------------------------------------------------
# Task 7 skip contract (≤5-line check must work on the produced message)
# --------------------------------------------------------------------------


def _is_subagent_completion(msg) -> bool:
    meta = getattr(msg, "metadata", None) or {}
    return bool(meta.get("internal")) and meta.get("provenance") == "subagent_completion"


def test_task7_skip_check_matches_builtin_message():
    assert _is_subagent_completion(build_completion_message(_make_run(), "p", "completed"))


def test_task7_skip_check_ignores_plain_messages():
    assert not _is_subagent_completion(HumanMessage(content="regular user message"))
    assert not _is_subagent_completion(HumanMessage(content="x", metadata={"internal": True}))
