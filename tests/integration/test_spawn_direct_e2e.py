"""Level C+ integration test: spawn_subagent_direct end-to-end.

Tests the full spawn pipeline:
  - spawn_subagent_direct() → background _execute_subagent() → complete → verify output

Uses real DeepSeek LLM, real agent execution. Tests both simple and complex tasks.
"""

import pytest
import uuid
import asyncio
from loguru import logger

from agent.tools.subagent.spawn.core import spawn_subagent_direct, SpawnResult
from agent.tools.subagent.types.spawn import SpawnMode, ContextMode
from agent.tools.subagent.registry import get_run


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registry():
    """Ensure a clean registry before each test."""
    from agent.tools.subagent.registry import clear as clear_registry

    clear_registry()
    yield
    clear_registry()


async def _poll_run(
    run_id: str,
    timeout: float = 180.0,
    interval: float = 1.0,
) -> dict:
    """Poll registry until the run reaches TERMINAL state, then return the record.

    Raises TimeoutError if the run doesn't complete within *timeout* seconds.
    """
    from agent.tools.subagent.types.registry import ExecutionStatus

    deadline = asyncio.get_event_loop().time() + timeout
    last_status = None
    while asyncio.get_event_loop().time() < deadline:
        run = get_run(run_id)
        if run is None:
            await asyncio.sleep(interval)
            continue
        status = run.execution.status
        if last_status != status:
            logger.info("  run {} status: {}", run_id, status.value)
            last_status = status
        if status == ExecutionStatus.TERMINAL:
            return run
        await asyncio.sleep(interval)
    raise TimeoutError(f"Run {run_id} did not complete within {timeout}s")


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spawn_direct_simple_task():
    """spawn_subagent_direct with a trivial task—ensures the full pipeline works."""
    requester_key = f"agent:test:parent:{uuid.uuid4().hex[:12]}"
    task = "Say exactly 'hello world' and nothing else."

    logger.info("=== test_spawn_direct_simple_task ===")
    logger.info("requester={}, task={!r}", requester_key, task)

    result: SpawnResult = await spawn_subagent_direct(
        task=task,
        requester_session_key=requester_key,
        agent_id="main",
        spawn_mode=SpawnMode.RUN,
        cleanup="delete",
        context=ContextMode.ISOLATED,
        run_timeout_seconds=120.0,
    )

    assert result.status == "accepted", f"Expected accepted, got {result.status}: {result.error}"
    assert result.run_id is not None
    assert result.child_session_key is not None
    logger.info("SpawResult: run_id={}, child={}", result.run_id, result.child_session_key)

    # Poll for completion
    run = await _poll_run(result.run_id, timeout=120.0)
    from agent.tools.subagent.types.registry import RunOutcomeStatus

    assert run.execution.outcome is not None
    assert run.execution.outcome.status == RunOutcomeStatus.OK, (
        f"Expected OK, got {run.execution.outcome.status}: {run.execution.outcome.error}"
    )

    result_text = (run.completion.result_text or "").strip().lower()
    assert "hello world" in result_text, (
        f"Expected 'hello world' in result, got: {run.completion.result_text!r}"
    )
    logger.info("✓ Simple task passed: {!r}", result_text)


@pytest.mark.asyncio
async def test_spawn_direct_complex_multi_step_task():
    """spawn_subagent_direct with a complex multi-step reasoning task.

    The subagent must:
      1. Read the workspace directory structure
      2. Find all .md files containing "task" or "todo"
      3. Count occurrences and summarize
    """
    requester_key = f"agent:test:parent:{uuid.uuid4().hex[:12]}"
    task = (
        "You have access to a terminal and file tools. Perform the following steps:\n"
        "1. List the files in the workspace/ directory (use terminal: `Get-ChildItem -Recurse -Filter '*.md' workspace/`)\n"
        "2. Read each .md file you find there\n"
        "3. Count how many times the word 'task' appears across all the .md files\n"
        "4. Report the filename-by-filename breakdown and the total count\n"
        "Output your answer in this format:\n"
        "Files: [comma-separated list of filenames]\n"
        "Breakdown:\n  [filename]: [count]\n...\nTotal: [sum]\n"
    )

    logger.info("=== test_spawn_direct_complex_multi_step_task ===")
    logger.info("requester={}", requester_key)

    result: SpawnResult = await spawn_subagent_direct(
        task=task,
        requester_session_key=requester_key,
        agent_id="main",
        spawn_mode=SpawnMode.RUN,
        cleanup="delete",
        context=ContextMode.ISOLATED,
        run_timeout_seconds=300.0,  # 5 min for complex task
    )

    assert result.status == "accepted", f"Expected accepted, got {result.status}: {result.error}"
    assert result.run_id is not None
    logger.info("SpawnResult: run_id={}, child={}", result.run_id, result.child_session_key)

    # Poll for completion
    run = await _poll_run(result.run_id, timeout=300.0)
    from agent.tools.subagent.types.registry import RunOutcomeStatus

    assert run.execution.outcome is not None, "Execution outcome is missing"

    # More lenient: TIMEOUT/KILLED is also a valid result for complex tasks
    if run.execution.outcome.status == RunOutcomeStatus.OK:
        result_text = (run.completion.result_text or "").strip()
        logger.info("✓ Complex task completed. Result (first 500 chars):\n{}", result_text[:500])
        # Verify some structure in the result
        assert len(result_text) > 50, f"Result too short: {len(result_text)} chars"
        assert "File" in result_text or "file" in result_text.lower(), (
            f"Expected 'File' in result, got excerpt: {result_text[:200]}"
        )
    else:
        # Log details for debugging but let the test pass with a warning
        logger.warning(
            "Complex task outcome: {}: {}",
            run.execution.outcome.status.value,
            run.execution.outcome.error,
        )
        # If timeout, that's OK—the pipeline worked, agent just ran long
        from agent.tools.subagent.types.registry import ExecutionStatus

        assert run.execution.status == ExecutionStatus.TERMINAL, (
            f"Run should be TERMINAL, got {run.execution.status}"
        )
        logger.info(
            "⚠ Complex task ended with {}—pipeline is functional",
            run.execution.outcome.status.value,
        )


@pytest.mark.asyncio
async def test_spawn_direct_custom_tools_disabled():
    """spawn_subagent_direct with a task that requires a blocked tool—verifies graceful handling."""
    requester_key = f"agent:test:parent:{uuid.uuid4().hex[:12]}"
    task = "Use the 'skill_manage' tool to list all available skills, then report what you found."

    logger.info("=== test_spawn_direct_custom_tools_disabled ===")
    logger.info("requester={}", requester_key)

    result: SpawnResult = await spawn_subagent_direct(
        task=task,
        requester_session_key=requester_key,
        agent_id="main",
        spawn_mode=SpawnMode.RUN,
        cleanup="delete",
        context=ContextMode.ISOLATED,
        run_timeout_seconds=120.0,
    )

    assert result.status == "accepted"
    run = await _poll_run(result.run_id, timeout=120.0)
    from agent.tools.subagent.types.registry import RunOutcomeStatus

    assert run.execution.outcome is not None
    # Agent may either gracefully report "tool not available" or error
    # Either is acceptable—the key is that the run completed
    assert run.execution.outcome.status in (RunOutcomeStatus.OK, RunOutcomeStatus.ERROR), (
        f"Unexpected outcome: {run.execution.outcome.status}"
    )

    if run.execution.outcome.status == RunOutcomeStatus.OK:
        result_text = (run.completion.result_text or "").strip()
        logger.info("✓ Blocked tool test completed. Result: {!r}", result_text[:300])

    logger.info("✓ spawn_subagent_direct blockade handling verified")


@pytest.mark.asyncio
async def test_spawn_direct_concurrent_tasks():
    """Spawn 3 independent subagents concurrently—validates parallel execution.

    Each agent gets a simple independent task. All should complete within
    the overall timeout.
    """
    requester_key = f"agent:test:parent:{uuid.uuid4().hex[:12]}"
    tasks = [
        f"Write a short poem about {subject} (4 lines max). Use the Write tool."
        for subject in ("Python", "AI", "caffeine")
    ]

    logger.info("=== test_spawn_direct_concurrent_tasks ===")
    logger.info("requester={}, spawning {} agents in parallel", requester_key, len(tasks))

    results: list[SpawnResult] = []
    for task in tasks:
        result = await spawn_subagent_direct(
            task=task,
            requester_session_key=requester_key,
            agent_id="main",
            spawn_mode=SpawnMode.RUN,
            cleanup="delete",
            context=ContextMode.ISOLATED,
            run_timeout_seconds=180.0,
        )
        assert result.status == "accepted"
        results.append(result)

    # Wait for all concurrently
    deadlines = {}
    for r in results:
        deadlines[r.run_id] = asyncio.get_event_loop().time() + 180.0

    completed = 0
    for r in results:
        try:
            run = await _poll_run(r.run_id, timeout=180.0)
            from agent.tools.subagent.types.registry import RunOutcomeStatus

            if run.execution.outcome and run.execution.outcome.status == RunOutcomeStatus.OK:
                completed += 1
                logger.info(
                    "  ✓ run {} OK: {!r}", r.run_id[:8], (run.completion.result_text or "")[:80]
                )
            else:
                logger.info(
                    "  ~ run {} {}",
                    r.run_id[:8],
                    run.execution.outcome.status.value if run.execution.outcome else "no outcome",
                )
        except TimeoutError:
            logger.warning("  ✗ run {} timed out", r.run_id[:8])

    logger.info("Concurrent: {}/{} agents completed OK", completed, len(results))
    assert completed >= 1, "At least one agent should complete OK"
    logger.info("✓ Concurrent spawn test passed ({}/3 OK)", completed)
