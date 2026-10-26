"""Real-store regression: an announce advances the TaskFlow that owns the child.

The auto-resume path used to pass the CHILD session key as ``session_id`` to
``taskflow_resume``; its scoped ``get_flow`` then missed the parent-owned flow
and the resume silently failed. This test uses the real taskflow store (no
mocked resume), so the owner-session binding is actually exercised.

The auto-resume owner fix is NOT part of the reverted per-scenario orphan
cleanup: without it the unreferenced-flow GC's live flows never advance when a child
completes.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from agent.tools.taskflow.config import StepStatus
from agent.tools.taskflow.registry import store_sqlite

pytestmark = [pytest.mark.unit]

delivery = importlib.import_module("agent.tools.subagent.announce.delivery")


def _run(child_key: str = "agent:main:subagent:c1", result: str = "the result"):
    return SimpleNamespace(
        run_id="run-1",
        child_session_key=child_key,
        completion=SimpleNamespace(result_text=result, required=True),
    )


@pytest.mark.asyncio
async def test_auto_resume_advances_the_real_flow(isolated_db):
    await store_sqlite.create_flow(
        "flow-auto",
        {
            "steps": [
                {
                    "step_id": "s1",
                    "task": "do the work",
                    "status": StepStatus.DISPATCHED.value,
                    "child_session_key": "agent:main:subagent:c1",
                }
            ],
            "results": [],
        },
        session_id="sess-parent",
    )

    assert await delivery._try_auto_resume_taskflow(_run()) is True

    flow = await store_sqlite.get_flow("flow-auto", "sess-parent")
    assert flow is not None
    assert flow["state"]["steps"][0]["status"] == StepStatus.DONE.value
    assert flow["state"]["results"][0]["result"] == "the result"
