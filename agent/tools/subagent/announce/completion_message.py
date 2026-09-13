"""Synthetic completion message builder for subagent-completion injections.

Builds the human-role message injected into the parent agent's turn input when
a child subagent run finishes (plan Q6: idle=synthetic message auto-turn /
busy=steering both consume this builder's output).

Frozen metadata contract (plan decision Q6) — carried on the LangChain
``BaseMessage.metadata`` field (native ``dict`` in langchain-core 1.4.7), so
downstream filters (RepetitionGuardWrapper / IterationBudget, task 7) skip
these messages with a trivial check:

    meta = getattr(msg, "metadata", None) or {}
    if meta.get("internal") and meta.get("provenance") == "subagent_completion":
        ...

Pure function of its inputs: no status auto-detection, no retry/analysis
logic. Status must be one of the plan vocabulary values below; mapping from
registry ``RunOutcomeStatus`` (ok/error/timeout/killed) is the caller's duty
(the registry has no 'completed'/'failed' enum values).
"""

from langchain_core.messages import HumanMessage

from ..types.registry import SubagentRunRecord

__all__ = [
    "PROVENANCE",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_INTERRUPTED",
    "VALID_STATUSES",
    "build_completion_message",
]

PROVENANCE = "subagent_completion"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"

VALID_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_INTERRUPTED})


def _resolve_child_name(child_run_info: SubagentRunRecord) -> str:
    """Resolve the display name for the marker line.

    Duck-typed so both ``SubagentRunRecord`` (label/task_name) and the task 5
    ``PendingInjection`` record (child_name) work without import coupling.
    """
    name = (
        getattr(child_run_info, "child_name", None)
        or getattr(child_run_info, "label", None)
        or getattr(child_run_info, "task_name", None)
    )
    return name or "unknown"


def build_completion_message(
    child_run_info: SubagentRunRecord,
    content: str | None,
    status: str,
) -> HumanMessage:
    """Build the synthetic human-role message announcing a child run's outcome.

    Text: ``[subagent:{name} {status}]`` followed by a newline and the content
    (marker line only when content is empty). Metadata carries exactly the
    frozen contract: internal=True, provenance='subagent_completion', run_id,
    status.

    Raises ``ValueError`` for any status outside ``VALID_STATUSES`` — fail fast
    keeps the parent-visible text format guaranteed.
    """
    if status not in VALID_STATUSES:
        raise ValueError(
            f"invalid subagent completion status {status!r}; expected one of {sorted(VALID_STATUSES)}"
        )

    name = _resolve_child_name(child_run_info)
    marker = f"[subagent:{name} {status}]"
    text = f"{marker}\n{content}" if content else marker

    return HumanMessage(
        content=text,
        metadata={
            "internal": True,
            "provenance": PROVENANCE,
            "run_id": child_run_info.run_id,
            "status": status,
        },
    )
