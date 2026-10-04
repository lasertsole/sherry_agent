"""Merge an isolated run's workspace back before its result is announced.

Called once per announce flow (which is the single point where a run turns
terminal — direct completion, orphan recovery, or the settle-wake retry all go
through it). The merge itself is blocking and takes a cross-process lock, so it
runs in a worker thread. Everything here is fail-open: an announce must never be
lost because a merge could not run, and the workspace stays on disk for a later
inspection when anything went wrong.

Ordering matters: the merge runs BEFORE the silent-reply check on purpose. A
child that answers ``⟦ANNOUNCE_SKIP⟧`` (or whose announce is suppressed) still
did real work in its copy, and that work must reach the parent tree even when no
completion message is delivered.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from ..isolation import MergeReport, isolated_workspace_meta, merge_isolated_workspace
from ..registry.session_keys import normalize_session_key
from ..types.registry import CompletionState, SubagentRunRecord

__all__ = ["attach_merge_report", "merge_isolated_workspace_for_run"]


async def merge_isolated_workspace_for_run(run: SubagentRunRecord) -> MergeReport | None:
    """Merge the run's isolated workspace, when it had one.

    :returns: The report, or ``None`` when the run was not isolated (or the
        merge could not run — the failure is logged, never raised).
    """
    meta_dir = isolated_workspace_meta(getattr(run, "spawned_cwd", None))
    if meta_dir is None:
        return None

    try:
        report = await asyncio.to_thread(merge_isolated_workspace, meta_dir)
    except Exception as exc:
        logger.opt(exception=exc).error(
            "Isolated workspace merge failed: run={} workspace={}", run.run_id, meta_dir
        )
        return None

    _mark_merged_paths_stale(run, report)
    logger.info(
        "Isolated workspace merged for run {}: {} (parent={})",
        run.run_id,
        report.summary(),
        report.parent_root,
    )
    return report


def _mark_merged_paths_stale(run: SubagentRunRecord, report: MergeReport) -> None:
    """The parent's verified evidence about a merged path is stale again."""
    from agent.tools.todolist.evidence_recorder import mark_evidence_stale

    parent_session_id = normalize_session_key(run.spawned_by or run.requester_session_key)
    for relpath in (*report.applied, *report.created, *report.deleted):
        mark_evidence_stale(str(report.parent_root / relpath), parent_session_id)


def attach_merge_report(run: SubagentRunRecord, report: MergeReport) -> SubagentRunRecord | None:
    """Append the merge report to the completion reply the parent will read.

    :returns: The updated run record, or ``None`` when there is nothing to
        attach to (no captured reply) or the registry write failed.
    """
    reply = run.completion.result_text
    if not reply or not reply.strip():
        return None

    block = _report_block(report)
    if block in reply:
        return None
    from ..registry.memory import update as update_run

    updated = update_run(
        run.run_id,
        completion=CompletionState(
            required=run.completion.required,
            result_text=f"{reply.rstrip()}\n\n{block}",
            captured_at=run.completion.captured_at,
        ),
    )
    return updated


def _report_block(report: MergeReport) -> str:
    """The markdown block appended to a completion reply."""
    lines = [
        "### Isolated workspace merged",
        f"- {report.summary()} → {report.parent_root}",
    ]
    if report.skipped:
        lines.append(f"- Skipped (never merged through): {len(report.skipped)} path(s)")
    if report.conflicts:
        lines.append(
            "- Conflicts (the parent's copy was left untouched; the isolated tree is "
            f"kept at `{report.parent_root}`'s workspace for inspection):"
        )
        lines.extend(f"  - `{path}`: {reason}" for path, reason in report.conflicts[:20])
        if len(report.conflicts) > 20:
            lines.append(f"  - … and {len(report.conflicts) - 20} more")
    return "\n".join(lines)
