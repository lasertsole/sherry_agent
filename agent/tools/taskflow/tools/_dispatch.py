"""Shared, monkeypatchable dispatch seam for the taskflow tool family.

The subagent runtime is imported lazily inside ``dispatch_child`` so this
module stays importable without the spawn pipeline (the family's standalone
load contract). Tools MUST call it module-qualified (``_dispatch.dispatch_child``)
so tests can substitute the seam without touching the real pipeline.

``flow_dispatch_lock`` is the process-local serialization point for one flow's
dispatch critical section: the manual ``taskflow_dispatch`` and the resume's
auto-dispatch wave both enter it BEFORE reading the flow, so two dispatchers
cannot both observe a step as ``ready`` and spawn it. The key includes the
running loop id, so tests that create a fresh loop per test never share a lock
across loops; the weak values let an idle flow's lock disappear.
"""

from __future__ import annotations

import asyncio
import threading
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

_DISPATCH_LOCKS: weakref.WeakValueDictionary[tuple[int, str], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)
_DISPATCH_LOCKS_GUARD = threading.Lock()


@asynccontextmanager
async def flow_dispatch_lock(flow_id: str) -> AsyncIterator[None]:
    """Serialize dispatch work for ``flow_id`` on the current event loop.

    Process-local by design: a single backend process owns a flow's dispatches,
    and TaskFlow tools are main-only. A multi-process deployment would need a
    DB-backed lease, not a wider in-process lock.
    """
    key = (id(asyncio.get_running_loop()), flow_id)
    with _DISPATCH_LOCKS_GUARD:
        lock = _DISPATCH_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _DISPATCH_LOCKS[key] = lock
    async with lock:
        yield


async def dispatch_child(
    task: str,
    requester_session_key: str,
    label: str | None = None,
    functional_role: str | None = None,
    output_schema: dict | None = None,
    step_model: str | None = None,
    step_timeout_seconds: float | None = None,
) -> str:
    """Dispatch a detached child session and return its child_session_key.

    ``functional_role`` optionally selects a functional specialization
    (general / researcher / executor / reviewer / librarian); ``None`` keeps the
    default spawn path. ``output_schema`` is the step's expectation contract: it
    is handed to the child as a structured-output requirement (validated again on
    resume, Tier 1). ``step_model`` overrides the child model and
    ``step_timeout_seconds`` the child run timeout; both ``None`` = spawn defaults.

    Raises RuntimeError unless the spawn is accepted with a child key.
    """
    from agent.tools.subagent import spawn_subagent_direct

    result = await spawn_subagent_direct(
        task=task,
        requester_session_key=requester_session_key,
        label=label,
        expects_completion_message=True,
        functional_role_hint=functional_role,
        output_schema=output_schema,
        model=step_model,
        run_timeout_seconds=step_timeout_seconds,
    )
    if result.status != "accepted" or not result.child_session_key:
        raise RuntimeError(f"status={result.status} error={result.error}")
    return result.child_session_key
