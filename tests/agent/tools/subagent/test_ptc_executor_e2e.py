"""Real-LLM e2e: an EXECUTOR child actually runs ``execute_code`` (PTC).

The hermetic PTC suite pins the injection wiring, the sandbox builtins, and the
RPC bridge; only a real model can prove the EXECUTOR child *chooses* to call
``execute_code`` and obtains a result reachable solely through that sandbox.

The task asks the child to run a script via ``execute_code`` that reads the
repository ``README.md`` through ``from sherry_tools import read_file`` and
prints its ``total_lines``. Two properties make the number unforgeable by the
model's own arithmetic:

* inside the PTC child process the builtin ``open`` is absent (restricted
  builtins — ``agent/tools/ptc/builtins.py``), so the file can only be read
  through the ``read_file`` RPC round-trip back to this process;
* the expected count is computed here at runtime from the same file, so the
  assertion stays correct across future README edits instead of hardcoding 451.

The test wraps ``run_ptc`` to capture each sandbox invocation, its script, and
its JSON envelope. Requiring an envelope with ``status == "ok"`` and
``tool_calls_made >= 1`` proves the RPC tool call was actually dispatched (not
merely narrated); the expected count must then appear in both the sandbox output
and the child's final ``## Task Result``.

Runs only under the dedicated real-LLM job (``llm_e2e`` marker).
"""

import asyncio
import json
import re
import uuid
from typing import Any

import pytest
from loguru import logger

from agent.tools.subagent.registry import get_run
from agent.tools.subagent.spawn.core import SpawnResult, spawn_subagent_direct

pytestmark = [pytest.mark.integration]

_TARGET = "README.md"


def _expected_total_lines() -> int:
    """Count README.md's lines exactly the way ``read_file`` does."""
    from config import ROOT_DIR

    raw = (ROOT_DIR / _TARGET).read_text(encoding="utf-8")
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    return len(raw.splitlines(keepends=True))


def _install_real_read_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expose the genuine ``read_file`` tool to the child's builder.

    ``tests/agent/tools/subagent/conftest.py`` replaces
    ``agent.tools.build_main_tools`` with ``lambda: []`` at import time, and
    ``_build_child_agent`` builds the child's tool set from it. The RESEARCHER
    reference test sidesteps this because code-intel tools are injected
    independently; the EXECUTOR path needs a real ``read_file`` for PTC to
    expose over RPC, so install the exact tool class production builds.
    """
    import agent.tools as agent_tools_stub
    from agent.tools.file_tools import build_read_file_tool

    monkeypatch.setattr(agent_tools_stub, "build_main_tools", lambda: [build_read_file_tool()])


@pytest.fixture(autouse=True)
def _clean_registry():
    """Ensure a clean registry before each test."""
    from agent.tools.subagent.registry import clear as clear_registry

    clear_registry()
    yield
    clear_registry()


async def _poll_run(run_id: str, timeout: float = 180.0, interval: float = 1.0):
    """Poll the registry until the run is TERMINAL, else raise ``TimeoutError``."""
    from agent.tools.subagent.types.registry import ExecutionStatus

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        run = get_run(run_id)
        if run is not None and run.execution.status == ExecutionStatus.TERMINAL:
            return run
        await asyncio.sleep(interval)
    raise TimeoutError(f"Run {run_id} did not complete within {timeout}s")


# CI time budget: the run budget is 180s and the poll another 180s; 300s bounds a
# hang while leaving headroom for the auxiliary-tier executor model.
@pytest.mark.timeout(300)
@pytest.mark.llm_e2e
@pytest.mark.asyncio
async def test_executor_subagent_runs_execute_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """An EXECUTOR child must call ``execute_code`` and read the file over RPC."""
    from agent.tools.ptc import tool as ptc_tool

    expected_lines = _expected_total_lines()
    logger.info(
        "=== test_executor_subagent_runs_execute_code (README={} lines) ===", expected_lines
    )

    recorded: list[dict[str, Any]] = []
    real_run_ptc = ptc_tool.run_ptc

    async def _spy_run_ptc(
        code: str,
        tools_map: dict[str, Any],
        session_id: str,
        config: Any,
        **kwargs: Any,
    ) -> str:
        raw = await real_run_ptc(code, tools_map, session_id, config, **kwargs)
        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            envelope = {}
        recorded.append({"code": code, "raw": raw, "envelope": envelope})
        return raw

    # Test-only observation: delegate to the real PTC runtime, changing no semantics.
    monkeypatch.setattr(ptc_tool, "run_ptc", _spy_run_ptc)
    _install_real_read_file(monkeypatch)

    requester_key = f"agent:test:parent:{uuid.uuid4().hex[:12]}"
    task = (
        "Use the `execute_code` tool exactly once to run this Python script:\n\n"
        "    from sherry_tools import read_file\n"
        "    import json\n"
        f'    data = json.loads(read_file("{_TARGET}"))\n'
        '    print(data["total_lines"])\n\n'
        "Inside that sandbox the builtin `open()` does not exist; `read_file` from\n"
        "`sherry_tools` is the only way to read the file (it is dispatched over RPC).\n"
        "Do NOT use a plain `read_file` tool call instead of `execute_code`.\n\n"
        "Then report the exact integer your script printed, ending with:\n\n"
        "## Task Result\n"
        f"{_TARGET} total_lines = <the integer printed above>"
    )

    result: SpawnResult = await spawn_subagent_direct(
        task=task,
        requester_session_key=requester_key,
        agent_id="main",
        cleanup="delete",
        run_timeout_seconds=180.0,
        functional_role_hint="executor",
    )

    assert result.status == "accepted", f"Expected accepted, got {result.status}: {result.error}"
    run = await _poll_run(result.run_id, timeout=180.0)

    from agent.tools.subagent.types.registry import RunOutcomeStatus

    assert run.execution.outcome is not None
    assert run.execution.outcome.status == RunOutcomeStatus.OK, (
        f"Expected OK, got {run.execution.outcome.status}: {run.execution.outcome.error}"
    )

    # 1. execute_code really ran: the spy captured a sandbox call whose script
    #    went through the sherry_tools read_file RPC bridge.
    assert recorded, "execute_code was never invoked by the EXECUTOR child"
    sandbox_calls = [
        rec for rec in recorded if "sherry_tools" in rec["code"] and "read_file" in rec["code"]
    ]
    assert sandbox_calls, (
        "execute_code ran but its script did not use the sherry_tools read_file RPC: "
        + repr([rec["code"][:300] for rec in recorded])
    )

    # 2. The envelope proves an RPC tool call was dispatched and the printed
    #    output carries the read_file-derived line count.
    ok_calls = [rec for rec in sandbox_calls if rec["envelope"].get("status") == "ok"]
    assert ok_calls, "execute_code finished without status=ok: " + repr(
        [(rec["envelope"].get("status"), rec["envelope"].get("error")) for rec in recorded]
    )
    assert any(int(rec["envelope"].get("tool_calls_made") or 0) >= 1 for rec in ok_calls), (
        "execute_code reported no RPC tool calls — read_file was not dispatched"
    )
    number = re.compile(rf"(?<!\d){expected_lines}(?!\d)")
    assert any(number.search(str(rec["envelope"].get("output") or "")) for rec in ok_calls), (
        f"execute_code output did not contain the actual README line count {expected_lines}: "
        + repr([rec["envelope"].get("output", "")[:200] for rec in ok_calls])
    )

    # 3. The child surfaced that number in its final answer.
    result_text = run.completion.result_text or ""
    logger.info("Child final result (first 500 chars):\n{}", result_text[:500])
    assert number.search(result_text), (
        f"EXECUTOR child did not report the README line count {expected_lines}: "
        f"{result_text[:400]!r}"
    )
