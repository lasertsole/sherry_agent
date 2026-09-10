"""Sisyphus completion verifier (E5): record evidence for a finished checkbox.

This module implements the evidence-recording half of the 5-gate completion
contract (``TODO/TODOLIST_PLAN.md`` §9 E5). Gates 2 (automated commands) and 3
(manual QA) are executed by the orchestrator outside this module: the verifier
rereads the plan (Gate 1), records the ``Verification:`` commands it finds,
probes the code-level adversarial classes it can observe here — TaskFlow step
status plus subagent run liveness (Gate 4) — reserves the cleanup receipt list
(Gate 5), and appends one ``task-completed`` entry to the evidence ledger.

Every external read is a **module-level injectable reference** so tests can
substitute fakes without touching real TaskFlow/subagent state. ``verify``
never raises: any failure is reported as ``(False, evidence)`` carrying a
human-readable ``reason``.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime

from loguru import logger

from .evidence_ledger import EvidenceLedger

__all__ = ["SisyphusVerifier"]

# TaskFlow's terminal step status; a linked step must reach it before the
# checkbox may be recorded as completed.
_STEP_DONE = "done"

# Matches a markdown checkbox line regardless of checked state (``[ ]``/``[x]``).
_CHECKBOX_RE = re.compile(r"^\s*-\s*\[[ xX]\]")


def _read_plan(plan_path: str) -> str:
    """Read the plan file; a missing/unreadable file is an empty string."""
    try:
        with open(plan_path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        logger.warning("sisyphus verifier: cannot read plan {}: {}", plan_path, e)
        return ""


def _extract_acceptance_criteria(plan_content: str, checkbox_label: str) -> str | None:
    """Return the indented acceptance/verification lines under a checkbox.

    A checkbox matches when it is a markdown task line (checked or not) whose
    text contains ``checkbox_label``. The following indented, non-blank lines
    up to the next checkbox form the acceptance block; ``None`` when the label
    is absent or it carries no indented criteria.
    """
    lines = plan_content.split("\n")
    for i, line in enumerate(lines):
        if not (_CHECKBOX_RE.match(line) and checkbox_label in line):
            continue
        criteria: list[str] = []
        for j in range(i + 1, len(lines)):
            nxt = lines[j]
            if not nxt.strip() or _CHECKBOX_RE.match(nxt):
                break
            if nxt.startswith(" ") or nxt.startswith("\t"):
                criteria.append(nxt.strip())
                continue
            break
        return "\n".join(criteria) if criteria else None
    return None


def _extract_verification_commands(acceptance: str) -> list[str]:
    """Collect the command text after each ``Verification:`` acceptance line.

    The verifier RECORDS these commands for the caller; it never runs them.
    Surrounding backticks are stripped so the ledger stores a runnable command.
    """
    commands: list[str] = []
    for line in acceptance.splitlines():
        stripped = line.strip().removeprefix("-").strip()
        if not stripped.lower().startswith("verification:"):
            continue
        command = stripped.split(":", 1)[1].strip().strip("`").strip()
        if not command:
            continue
        commands.append(command)
    return commands


async def _load_flow(flow_id: str) -> dict | None:
    """Injectable seam: read-only TaskFlow flow lookup (never schedules)."""
    from agent.tools.taskflow.registry import get_flow

    return await get_flow(flow_id)


def _step_status(step: dict) -> str:
    """Injectable seam: derive a TaskFlow step's status (legacy-aware)."""
    from agent.tools.taskflow.tools._shared import step_status

    return step_status(step)


def _get_run_by_child_session_key(child_session_key: str):
    """Injectable seam: look up a subagent run record by child session key."""
    from agent.tools.subagent.registry import get_run_by_child_session_key

    return get_run_by_child_session_key(child_session_key)


def _is_live_unended_run(run) -> bool:
    """Injectable seam: True while the run is RUNNING or INTERRUPTED."""
    from agent.tools.subagent.registry import is_live_unended_run

    return is_live_unended_run(run)


async def _read_step_status(flow_id: str, step_id: str) -> tuple[str | None, str | None]:
    """Read a linked TaskFlow step's status, or the reason it is unresolvable.

    Returns ``(status, problem)`` with exactly one meaningful value. ``problem``
    is a human-readable reason when the flow is absent or the step is not in it;
    both are treated as "not done" by the barrier.
    """
    flow = await _load_flow(flow_id)
    if flow is None:
        return None, f"TaskFlow flow '{flow_id}' not found"
    steps = (flow.get("state") or {}).get("steps") or []
    for step in steps:
        if step.get("step_id") == step_id:
            return _step_status(step), None
    return None, f"TaskFlow step '{step_id}' not found in flow '{flow_id}'"


def _fail(evidence: dict, reason: str) -> tuple[bool, dict]:
    """Mark the evidence with a reason and return the failing pair."""
    evidence["reason"] = reason
    logger.info("sisyphus verifier: FAILED {}", reason)
    return False, evidence


async def _run_gates(
    evidence: dict, todo: dict, plan_path: str, checkbox_label: str
) -> tuple[bool, dict]:
    """Run the gates the verifier can execute and record their evidence.

    Gate 1 rereads the plan; Gate 2 records ``Verification:`` commands; Gate 4
    probes the code-level adversarial classes (TaskFlow step status + subagent
    liveness); Gate 5 reserves the cleanup receipt list. Gates 2 (run) and 3
    (manual QA) are the orchestrator's job — this function only records.
    """
    # Gate 1: Plan reread — locate the checkbox and its acceptance criteria.
    if not plan_path or not os.path.exists(plan_path):
        return _fail(evidence, f"plan not found: {plan_path!r}")
    acceptance = _extract_acceptance_criteria(_read_plan(plan_path), checkbox_label)
    if acceptance is None:
        return _fail(
            evidence,
            f"acceptance criteria not found for checkbox {checkbox_label!r} in {plan_path!r}",
        )
    evidence["adversarial_classes"]["plan_reread"] = "passed"

    # Gate 2 (record only): the plan's Verification: commands.
    evidence["commands"] = _extract_verification_commands(acceptance)

    # Gate 4: adversarial, code-level — linked TaskFlow step must be done.
    flow_id = todo.get("flow_id")
    step_id = todo.get("step_id")
    if flow_id and step_id:
        status, problem = await _read_step_status(str(flow_id), str(step_id))
        if problem is not None:
            return _fail(evidence, problem)
        if status != _STEP_DONE:
            return _fail(
                evidence,
                f"TaskFlow step {step_id} (flow {flow_id}) is '{status}'; "
                "call taskflow_wait_all then taskflow_resume before completing",
            )
        evidence["adversarial_classes"]["taskflow_step"] = str(status)

    # Gate 4: adversarial — a linked subagent run must no longer be live.
    subagent_id = todo.get("subagent_id")
    if subagent_id:
        run = _get_run_by_child_session_key(str(subagent_id))
        if run is not None and _is_live_unended_run(run):
            return _fail(
                evidence,
                f"subagent {subagent_id} is still running; wait for it to finish",
            )
        evidence["adversarial_classes"]["subagent_run"] = "not-live"

    # Gate 5: cleanup receipts are the caller's to fill; reserve the list.
    evidence["cleanup"] = []

    EvidenceLedger.append(evidence)
    logger.info("sisyphus verifier: PASSED {} ({})", checkbox_label, plan_path)
    return True, evidence


class SisyphusVerifier:
    """Reread/verify/record engine for the Sisyphus completion contract."""

    @staticmethod
    async def verify(
        session_id: str,
        todo: dict,
        plan_path: str,
        checkbox_label: str,
    ) -> tuple[bool, dict]:
        """Run the verifier's gates and append one evidence entry.

        Returns ``(passed, evidence)``. ``evidence`` always carries the
        ``task-completed`` event shape; on failure a human-readable ``reason``
        is added. This method never raises — an unexpected error is reported as
        a failed verdict.
        """
        evidence = {
            "event": "task-completed",
            "plan": plan_path,
            "task": checkbox_label,
            "session_id": session_id,
            "adversarial_classes": {},
            "commands": [],
            "cleanup": [],
            "timestamp": datetime.now(UTC).isoformat(),
        }
        try:
            return await _run_gates(evidence, todo, plan_path, checkbox_label)
        except Exception as e:  # noqa: BLE001 - the verifier must never raise
            return _fail(evidence, f"verifier error: {e}")
