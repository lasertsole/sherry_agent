"""Level C: True end-to-end integration test for _execute_subagent.

Uses real DeepSeek LLM, real SQLite checkpointer, real agent execution.
Completion delivery is disabled (completion.required=False) to skip the
MessageBus announce flow, but the run record is still written to the
in-memory registry so get_run() works after execution.
"""

import pytest
import uuid
from pathlib import Path

from agent.tools.subagent.spawn.core import _execute_subagent
from agent.tools.subagent.types.registry import (
    SubagentRunRecord,
    ExecutionState,
    CompletionState,
    CompletionDeliveryState,
    ExecutionStatus,
    DeliveryStatus,
    RunOutcomeStatus,
)
from agent.tools.subagent.types.spawn import SpawnMode, ContextMode
from agent.tools.subagent.types.capability import SubagentSessionRole, ControlScope
from agent.tools.subagent.registry import clear as clear_registry, get_run
from agent.tools.subagent.registry import memory as registry_memory


@pytest.fixture(autouse=True)
def _clean_registry():
    """Ensure a clean registry before each test."""
    clear_registry()
    yield
    clear_registry()


def _make_run(
    task: str,
    child_session_key: str | None = None,
    requester_session_key: str | None = None,
) -> SubagentRunRecord:
    """Build a minimal SubagentRunRecord for _execute_subagent."""
    csk = child_session_key or f"agent:test:subagent:{uuid.uuid4().hex[:12]}"
    rsk = requester_session_key or "agent:test:parent"
    return SubagentRunRecord(
        run_id=uuid.uuid4().hex[:12],
        task_run_id=uuid.uuid4().hex[:12],
        child_session_key=csk,
        requester_session_key=rsk,
        task=task,
        spawn_mode=SpawnMode.RUN,
        cleanup="delete",
        context_mode=ContextMode.ISOLATED,
        agent_id="main",
        depth=3,
        role=SubagentSessionRole.LEAF,
        control_scope=ControlScope.NONE,
        generation=0,
        controller_session_key=rsk,
        completion_owner_session_key=rsk,
        execution=ExecutionState(status=ExecutionStatus.RUNNING),
        completion=CompletionState(
            required=False,  # Skip MessageBus announce flow
            result_text=None,
        ),
        delivery=CompletionDeliveryState(
            status=DeliveryStatus.NOT_REQUIRED,
        ),
        inherited_tool_allow=[],
        inherited_tool_deny=[],
        scopes=[],
        spawned_by="test",
        spawned_cwd=str(Path.cwd()),
        expects_completion_message=False,
    )


# CI time budget: observed solo runtime ~13s (REPORT.md experiment A: 13.33s; experiment D: 9.54s).
# 300s ≈ 20x headroom while bounding a future hang — defense-in-depth for CI, NOT a replacement
# for the teardown fix (commit ea5872a already eliminated the exit hang without timeout masking).
@pytest.mark.timeout(300)
@pytest.mark.llm_e2e
@pytest.mark.asyncio
async def test_execute_subagent_simple_task() -> None:
    """Use a real DeepSeek call to say hello world and verify the result."""
    task = "Say exactly 'hello world' and nothing else."
    system_prompt = "You are a helpful assistant. Answer concisely."
    user_message = task

    run = _make_run(task=task)
    run_id = run.run_id
    print(f"\n[TEST] run_id={run_id}, child_session_key={run.child_session_key}")

    # Pre-register the run in the in-memory registry so that
    # complete_subagent_run() → get_run(run_id) can find it.
    registry_memory.set_run(run)

    await _execute_subagent(
        run=run,
        system_prompt=system_prompt,
        user_message=user_message,
        forked_messages=[],
        tools=None,  # Falls back to build_main_tools()
        timeout_seconds=120.0,
    )

    # Retrieve the completed run record — should still be in registry
    # after cleanup (cleanup deletes the session via MessageBus, not the run).
    completed = get_run(run_id)
    if completed is None:
        all_runs = registry_memory.values()
        print(
            f"[TEST] Run not found in registry. All runs ({len(all_runs)}): {[r.run_id for r in all_runs]}"
        )
        pytest.fail(f"Run {run_id} not found in registry after execution")
        return

    # Verify outcome
    assert completed.execution.status == ExecutionStatus.TERMINAL, (
        f"Expected TERMINAL, got {completed.execution.status}"
    )
    assert completed.execution.outcome is not None
    assert completed.execution.outcome.status == RunOutcomeStatus.OK, (
        f"Expected OK, got {completed.execution.outcome.status}: {completed.execution.outcome.error}"
    )

    # Verify result text
    result = (completed.completion.result_text or "").strip().lower()
    assert "hello world" in result, (
        f"Expected 'hello world' in result, got: {completed.completion.result_text!r}"
    )
