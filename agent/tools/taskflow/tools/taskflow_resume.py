"""taskflow_resume: inject a child session result into the flow state (openclaw resume).

Idempotency: the (child_session_key, result) pair is fingerprinted; resuming
with the SAME pair again is a no-op that neither re-injects nor bumps the
revision, so announce-pipeline redeliveries cannot corrupt the state.
"""

import hashlib
import time

from langchain_core.tools import tool

from ..config import TaskFlowStatus
from ..registry import store_sqlite
from ..registry.store_sqlite import FlowConflictError, FlowNotFoundError
from ._shared import conflict_error, is_terminal, not_found_error, terminal_error


def _result_hash(child_session_key: str, result: str) -> str:
    payload = f"{child_session_key}\x1f{result}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@tool("taskflow_resume")
async def taskflow_resume(
    flow_id: str,
    child_session_key: str = "",
    result: str = "",
    expected_revision: int | None = None,
) -> str:
    """Inject a completed child session result into the flow state (idempotent).

    Appends {child_session_key, result} to the flow's results and returns the
    flow to running when it was waiting. Resuming with the SAME
    child_session_key and result again is a no-op (already resumed), so
    duplicate deliveries never inject twice. Pass expected_revision to fail
    fast on concurrent writers.
    """
    flow_id = (flow_id or "").strip()
    child_session_key = (child_session_key or "").strip()
    result = result or ""
    if not flow_id:
        return "Error: flow_id is required"
    if not child_session_key and not result:
        return "Error: taskflow_resume requires child_session_key or result"

    flow = await store_sqlite.get_flow(flow_id)
    if flow is None:
        return not_found_error(flow_id)
    if is_terminal(flow["status"]):
        return terminal_error(flow_id, flow["status"])

    revision = (
        int(expected_revision) if expected_revision is not None else flow["expected_revision"]
    )

    result_hash = _result_hash(child_session_key, result)
    state = dict(flow["state"])
    results = list(state.get("results") or [])
    if any(
        isinstance(r, dict) and r.get("result_hash") == result_hash for r in results
    ):
        return (
            f"TaskFlow resume skipped (already resumed): flow_id={flow_id}, "
            f"result_hash={result_hash}, results={len(results)}"
        )

    results.append(
        {
            "child_session_key": child_session_key,
            "result": result,
            "result_hash": result_hash,
            "injected_at": time.time(),
        }
    )
    state["results"] = results

    # Resuming a waiting flow returns it to running; a running flow stays running.
    new_status = (
        TaskFlowStatus.RUNNING.value
        if flow["status"] == TaskFlowStatus.WAITING.value
        else None
    )

    try:
        updated = await store_sqlite.update_flow(
            flow_id,
            revision,
            state=state,
            wait=None,
            status=new_status,
        )
    except FlowConflictError as exc:
        return conflict_error(exc)
    except FlowNotFoundError:
        return not_found_error(flow_id)

    return (
        f"TaskFlow resumed: flow_id={flow_id}, revision={updated['expected_revision']}, "
        f"results={len(results)}, status={updated['status']}"
    )
