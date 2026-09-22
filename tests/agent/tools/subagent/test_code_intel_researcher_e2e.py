"""Real-LLM e2e: a RESEARCHER child actually invokes the code_intel tools.

Runs only under the dedicated ``--with-llm-e2e`` job (``llm_e2e`` marker) with
``MAIN_LLM_API_KEY`` set. It indexes a tiny tmp fixture repo (never the real
repository) via ``SHERRY_CODE_INTEL_ROOT`` and asks the child to call
``explore``; a plain LLM cannot know the synthetic path, so a correct file path
in the result proves the tool ran.
"""

import asyncio
import textwrap
import uuid
from pathlib import Path

import pytest
from loguru import logger

from agent.tools.subagent.spawn.core import spawn_subagent_direct, SpawnResult
from agent.tools.subagent.types.spawn import SpawnMode
from agent.tools.subagent.registry import get_run

pytestmark = [pytest.mark.integration]

_FIXTURE = {
    "pkg/main.py": '''
        def helper(x):
            return x + 1

        def top_func(x):
            """Top docstring."""
            return helper(x)
    ''',
    "web/app.ts": """
        export function topFunc(x: number): number {
          return helper(x);
        }
    """,
}


@pytest.fixture(autouse=True)
def _clean_registry():
    from agent.tools.subagent.registry import clear as clear_registry

    clear_registry()
    yield
    clear_registry()


@pytest.fixture()
def fixture_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    for rel, content in _FIXTURE.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    monkeypatch.setenv("SHERRY_CODE_INTEL_ROOT", str(root))
    monkeypatch.setenv("SHERRY_CODE_INTEL_DB", str(tmp_path / "codeintel" / "index.db"))
    return root


async def _poll_run(run_id: str, timeout: float = 180.0, interval: float = 1.0):
    from agent.tools.subagent.types.registry import ExecutionStatus

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        run = get_run(run_id)
        if run is not None and run.execution.status == ExecutionStatus.TERMINAL:
            return run
        await asyncio.sleep(interval)
    raise TimeoutError(f"Run {run_id} did not complete within {timeout}s")


@pytest.mark.timeout(300)
@pytest.mark.llm_e2e
@pytest.mark.asyncio
async def test_researcher_subagent_invokes_explore(fixture_repo: Path) -> None:
    """A RESEARCHER child must be able to call ``explore`` and report its result."""
    requester_key = f"agent:test:parent:{uuid.uuid4().hex[:12]}"
    task = (
        "Use the `explore` tool with query 'top_func' and report the exact file path it "
        "returns for the symbol. If the tool is unavailable, say so explicitly.\n"
        "Finish with a ## Task Result section."
    )

    logger.info("=== test_researcher_subagent_invokes_explore ===")
    result: SpawnResult = await spawn_subagent_direct(
        task=task,
        requester_session_key=requester_key,
        agent_id="main",
        spawn_mode=SpawnMode.RUN,
        cleanup="delete",
        run_timeout_seconds=180.0,
        functional_role_hint="researcher",
    )

    assert result.status == "accepted", f"Expected accepted, got {result.status}: {result.error}"
    run = await _poll_run(result.run_id, timeout=180.0)

    from agent.tools.subagent.types.registry import RunOutcomeStatus

    assert run.execution.outcome is not None
    assert run.execution.outcome.status == RunOutcomeStatus.OK, (
        f"Expected OK, got {run.execution.outcome.status}: {run.execution.outcome.error}"
    )
    result_text = run.completion.result_text or ""
    assert "pkg/main.py" in result_text, (
        f"RESEARCHER child did not surface the explore result: {result_text[:400]!r}"
    )
