"""taskflow_finish: mark the flow done (openclaw finish).

The finish path is gated before the DONE transition:

* Gate A — DAG completeness: every step is ``done`` or ``blocked`` (none may
  still be ``ready``/``dispatched``).
* Gate B — a ``blocked`` step needs intervention, not a completion record.
* Gate C — flow-scoped evidence must be free of failing/stale rows.
* Gate D — ``SisyphusVerifier``, only when the caller explicitly supplies the
  linked ``todo`` + ``plan_path`` (caller-supplied linkage; no schema migration).

Every gate is fail-open: an unavailable evidence collector or verifier never
blocks a finish, and a skipped Gate D is silent.
"""

from langchain_core.tools import tool
from loguru import logger

from ..config import StepStatus, TaskFlowStatus
from ..registry import store_sqlite
from ..progress_push import push_taskflow_progress
from ..registry.store_sqlite import FlowConflictError, FlowNotFoundError
from config.features import GOAL_GATE
from agent.tools.pub_base import SessionId
from ._shared import (
    conflict_error,
    is_terminal,
    not_found_error,
    step_status,
    terminal_error,
)


# Gate A passes a step in exactly these states; anything else is still pending.
_FINISHABLE_STATUSES = (StepStatus.DONE.value, StepStatus.BLOCKED.value)


async def _goal_gate(flow: dict) -> str | None:
    """Gate E: does the flow's result set actually satisfy the original goal?

    Gates A-D prove the DAG is finished and the evidence is clean; none of them
    reads the RESULTS against the goal the user stated. This gate asks an
    auxiliary model that one question, so a flow whose steps all "passed" their
    own criteria cannot close while the goal is unmet. Opt-in by construction:
    it runs only when the flow carries a ``description`` (the goal), and it is
    fail-open — a judge error passes, because a broken judge must never pin a
    flow open.

    Returns ``None`` to pass, else the rejection text (which names
    ``taskflow_replan`` so the model knows the next move).
    """
    if not GOAL_GATE["enabled"]:
        return None
    state = dict(flow.get("state") or {})
    goal = str(state.get("description") or flow.get("description") or "").strip()
    if not goal:
        return None  # no stated goal -> nothing to check (behaviour unchanged)

    results = [
        record
        for record in (state.get("results") or [])
        if isinstance(record, dict) and str(record.get("result") or "").strip()
    ]
    if not results:
        return None  # nothing to judge (a flow with results is the interesting case)

    joined = "\n---\n".join(
        f"[{record.get('step_id') or record.get('child_session_key') or 'step'}] "
        f"{str(record.get('result'))[: int(GOAL_GATE['max_result_chars'])]}"
        for record in results
    )
    prompt = (
        f"Goal:\n{goal}\n\nStep results:\n{joined}\n\n"
        "Does the collected work satisfy the goal? Answer with exactly one line: "
        "'GOAL_MET: <one-line reason>' or 'GOAL_NOT_MET: <what is missing>'."
    )
    try:
        # Imported at call time (like the other auxiliary-model call sites): the
        # constructor seam stays patchable and no model client is built at import.
        from models import build_auxiliary_llm

        llm = build_auxiliary_llm(temperature=0)
        response = await llm.ainvoke(
            [
                {"role": "system", "content": _GOAL_GATE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
        )
        raw = str(response.content if hasattr(response, "content") else response).strip()
    except Exception as exc:  # noqa: BLE001 - fail-open boundary of this gate
        logger.warning("TaskFlow goal gate skipped (judge error, failing open): {}", exc)
        return None

    upper = raw.upper()
    if "GOAL_NOT_MET" in upper:
        reason = raw.split(":", 1)[1].strip() if ":" in raw else raw
        logger.info("TaskFlow goal gate rejected the finish: {}", reason[:200])
        return (
            f"Error: Cannot finish: the goal is not met ({reason}). "
            "Re-plan the remaining work with taskflow_replan, or finish the missing "
            "steps and try again."
        )
    return None


_GOAL_GATE_SYSTEM_PROMPT = (
    "You verify whether a set of completed step results satisfies the stated goal. "
    "Be strict about missing deliverables, but do not invent requirements that were "
    "never asked for. Answer with exactly one line: 'GOAL_MET: <reason>' or "
    "'GOAL_NOT_MET: <what is missing>'."
)


async def _evidence_gate(flow_id: str) -> str | None:
    """Return an ``Error:`` text when the flow's evidence is failing/stale.

    Fail-open: a missing collector, unreadable ledger, or any collector error
    passes (``None``) — an optional probe must never block a finish.
    """
    try:
        from ..evidence_collector import collect_evidence_summary

        evidence = collect_evidence_summary(flow_id=flow_id)
    except Exception:  # noqa: BLE001 - optional probe; never block a finish
        return None
    if not evidence:
        return None
    if "FAIL" in evidence:
        return f"Error: Finish gate rejected: failing verification evidence:\n{evidence}"
    if "[stale]" in evidence:
        return (
            "Error: Finish gate rejected: stale evidence (code changed since last "
            f"verification):\n{evidence}\nRe-run verification commands before finishing."
        )
    return None


async def _verifier_gate(
    session_id: str,
    todo: dict | None,
    plan_path: str | None,
    checkbox_label: str | None,
) -> str | None:
    """Return an ``Error:`` text when ``SisyphusVerifier`` rejects a completion.

    Gate D runs only when the caller passes BOTH ``todo`` and ``plan_path``: the
    main agent that holds the todolist supplies the linkage explicitly, so no
    flow/step schema migration is required. A skipped gate and any verifier
    error are fail-open (``None``).
    """
    if not todo or not plan_path:
        return None
    try:
        from agent.tools.todolist.verifier import SisyphusVerifier

        passed, verify_evidence = await SisyphusVerifier.verify(
            session_id=session_id,
            todo=todo,
            plan_path=plan_path,
            checkbox_label=checkbox_label or "",
        )
    except Exception:  # noqa: BLE001 - optional probe; never block a finish
        return None
    if passed:
        return None
    reason = verify_evidence.get("reason", "unknown")
    return f"Error: Finish gate rejected by SisyphusVerifier: {reason}"


@tool("taskflow_finish")
async def taskflow_finish(
    flow_id: str,
    summary: str = "",
    expected_revision: int | None = None,
    todo: dict | None = None,
    plan_path: str | None = None,
    checkbox_label: str | None = None,
    session_id: SessionId = "",
) -> str:
    """Mark the flow as done; terminal, no further mutations are accepted.

    Records the summary in the flow state. Pass expected_revision to fail fast
    on concurrent writers. Finishing requires every step to be done or blocked
    (blocked steps reject), no failing/stale flow evidence, and — only when
    ``todo`` + ``plan_path`` are supplied — a passing SisyphusVerifier check.
    """
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"

    flow = await store_sqlite.get_flow(flow_id, session_id)
    if flow is None:
        return not_found_error(flow_id)
    if is_terminal(flow["status"]):
        return terminal_error(flow_id, flow["status"])

    revision = (
        int(expected_revision) if expected_revision is not None else flow["expected_revision"]
    )

    state = dict(flow["state"])
    steps = list(state.get("steps") or [])

    # Gate A — DAG completeness: no step may still be ready or dispatched.
    pending = [step for step in steps if step_status(step) not in _FINISHABLE_STATUSES]
    if pending:
        return (
            f"Error: Cannot finish: {len(pending)} step(s) not done/blocked: "
            f"{[step.get('step_id') for step in pending]}"
        )

    # Gate B — an unresolved step (blocked / failed / skipped / cancelled) needs
    # a decision, not a completion record. ``failed`` in particular must never be
    # finished over: its dependents are still blocked and the work is not done.
    blocked = [step for step in steps if step_status(step) == StepStatus.BLOCKED.value]
    failed = [
        step
        for step in steps
        if step_status(step)
        in (StepStatus.FAILED.value, StepStatus.SKIPPED.value, StepStatus.CANCELLED.value)
    ]
    if failed:
        details = [
            f"{step.get('step_id')}={step_status(step)}"
            f"({step.get('fail_reason') or step.get('skip_reason') or 'no reason recorded'})"
            for step in failed
        ]
        return (
            f"Error: Cannot finish: {len(failed)} step(s) unresolved: {details}. "
            "Resume, retry or cancel them, or cancel the flow."
        )
    if blocked:
        return (
            f"Error: Cannot finish: {len(blocked)} step(s) are blocked: "
            f"{[step.get('block_reason', 'unknown') for step in blocked]}"
        )

    # Gate C — flow-scoped evidence must be free of failing/stale rows.
    evidence_error = await _evidence_gate(flow_id)
    if evidence_error is not None:
        return evidence_error

    # Gate D — SisyphusVerifier, only when the caller supplied the linkage.
    verifier_error = await _verifier_gate(session_id, todo, plan_path, checkbox_label)
    if verifier_error is not None:
        return verifier_error

    # Gate E — the goal judge (opt-in: a flow with a description). Gates A-D
    # prove the machinery finished; this is the only gate that reads the
    # results against what the user actually asked for.
    goal_error = await _goal_gate(flow)
    if goal_error is not None:
        return goal_error

    if summary:
        state["summary"] = summary

    try:
        updated = await store_sqlite.update_flow(
            flow_id,
            revision,
            session_id=session_id,
            state=state,
            wait=None,
            status=TaskFlowStatus.DONE.value,
        )
    except FlowConflictError as exc:
        return conflict_error(exc)
    except FlowNotFoundError:
        return not_found_error(flow_id)

    await push_taskflow_progress(session_id)
    return (
        f"TaskFlow finished: flow_id={flow_id}, revision={updated['expected_revision']}, "
        f"status={updated['status']}"
    )
