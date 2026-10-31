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

    report.affected_importers = _affected_importers(report)
    _mark_merged_paths_stale(run, report)
    await _reverify_after_merge(run, report)
    _share_merge_finding(run, report)
    logger.info(
        "Isolated workspace merged for run {}: {} (parent={})",
        run.run_id,
        report.summary(),
        report.parent_root,
    )
    return report


def _affected_importers(report: MergeReport) -> list[str]:
    """Files that still import a symbol this merge renamed/removed/re-shaped.

    The report used to name the change and leave the caller hunt to the parent;
    the reverse index answers it directly. READ-ONLY and fail-open: a missing
    index (never built, or a project outside code-intel) yields no names and
    the report degrades to exactly the old behaviour — this must never trigger
    an index build inside a merge.
    """
    names: set[str] = set()
    for delta in report.interface_changes:
        names.update(change.name for change in delta.removed)
        names.update(change.old_name for change in delta.renamed)
        names.update(change.name for change in delta.changed)
    if not names:
        return []
    try:
        from agent.tools.code_intel.indexer import importers_of_names
        from agent.tools.code_intel.roots import index_db_for_root
        from config.features import CODE_INTEL
        from config.path import CODE_INTEL_DIR

        db_path = index_db_for_root(
            report.parent_root, str(CODE_INTEL.get("db_path") or ""), CODE_INTEL_DIR
        )
        merged = set(report.applied) | set(report.created)
        return [
            path
            for path in importers_of_names(db_path, names)
            if path not in merged  # the file that made the change is not a victim
        ]
    except Exception as exc:  # noqa: BLE001 - the report must render regardless
        logger.debug("affected-importer lookup skipped: {}", exc)
        return []


def _mark_merged_paths_stale(run: SubagentRunRecord, report: MergeReport) -> None:
    """The parent's verified evidence about a merged path is stale again.

    Importer files count too: a "pure import user" never writes or verifies the
    changed module, so a path-only sweep left its PASSING evidence green even
    though the symbol it imports just changed shape.
    """
    from agent.tools.pub_base.evidence_recorder import mark_evidence_stale_for_paths

    parent_session_id = normalize_session_key(run.spawned_by or run.requester_session_key)
    mark_evidence_stale_for_paths(
        (
            str(report.parent_root / relpath)
            for relpath in (
                *report.applied,
                *report.created,
                *report.deleted,
                *report.affected_importers,
            )
        ),
        parent_session_id,
    )


def _share_merge_finding(run: SubagentRunRecord, report: MergeReport) -> None:
    """Publish a PATCH finding so peers can read what this merge changed.

    Opt-in (``COORDINATION["findings_enabled"]``) and fail-open. The text is the
    interface summary the report already computed — the findings board is a
    lookup surface, never a prompt injection (live data in the system prompt
    would break the provider prefix cache).
    """
    from config.features import COORDINATION

    if not COORDINATION["findings_enabled"]:
        return
    try:
        from agent.tools.coordination_tools import share_merge_finding

        files = [*report.applied, *report.created]
        changed = [
            change.name
            for delta in report.interface_changes
            for change in (*delta.removed, *delta.renamed, *delta.changed)
        ]
        text = (
            f"merged {len(files)} file(s); interface changes: {', '.join(changed[:10])}"
            if changed
            else f"merged {len(files)} file(s); no interface changes"
        )
        parent_session_id = normalize_session_key(run.spawned_by or run.requester_session_key)
        share_merge_finding(report.parent_root, parent_session_id, files, text)
    except Exception as exc:  # noqa: BLE001 - the merge never fails on its notice
        logger.debug("merge finding skipped: {}", exc)


async def _reverify_after_merge(run: SubagentRunRecord, report: MergeReport) -> None:
    """Verdict-on-the-spot for the files the interface change may have broken.

    OFF by default (``EVIDENCE_REGRESSION_GATE["merge_reverify_enabled"]``): it
    runs a subprocess during an announce. When on, the parent session's passed
    commands covering the affected importers are replayed once, so the report's
    "affected importers" line is followed by a real broken/unmark event rather
    than leaving the discovery to a finish gate that may be many turns away.
    Fail-open; a skipped replay is not an error.
    """
    from config.features import EVIDENCE_REGRESSION_GATE

    if not EVIDENCE_REGRESSION_GATE["merge_reverify_enabled"] or not report.affected_importers:
        return
    try:
        from agent.tools.pub_base.regression_gate import reverify_affected_paths

        parent_session_id = normalize_session_key(run.spawned_by or run.requester_session_key)
        results = await reverify_affected_paths(
            parent_session_id,
            report.affected_importers,
            max_commands=int(EVIDENCE_REGRESSION_GATE["merge_reverify_max_commands"]),
        )
        report.merge_reverify = [
            f"{result.outcome}: {result.command}"
            for result in results
            if result.outcome != "passed"
        ]
    except Exception as exc:  # noqa: BLE001 - the merge report must still render
        logger.warning("merge re-verify skipped (fail-open): {}", exc)


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
    lines.extend(_interface_lines(report))
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


#: Rendering caps for the interface section: the parent agent needs the shape of
#: the change, not an exhaustive listing of a regenerated file.
_INTERFACE_MAX_FILES = 10
_INTERFACE_MAX_PER_KIND = 20


def _symbol_label(kind: str, parent: str | None) -> str:
    """``function`` / ``class`` / ``method of `Api``` — how a line ends."""
    if parent and kind == "method":
        return f"method of `{parent}`"
    if parent:
        return f"{kind} of `{parent}`"
    return kind


def _interface_lines(report: MergeReport) -> list[str]:
    """The ``Interface changes`` section, or ``[]`` when nothing changed."""
    deltas = report.interface_changes
    if not deltas:
        return []

    lines = [
        "- **Interface changes** (renamed/added/removed symbols, and call-shape changes — "
        "another subagent may still call the OLD name or the OLD arguments):"
    ]
    for delta in deltas[:_INTERFACE_MAX_FILES]:
        if delta.error:
            lines.append(f"  - `{delta.relpath}` ({delta.language}): not compared — {delta.error}")
            continue
        lines.append(f"  - `{delta.relpath}` ({delta.language}):")
        for rename in delta.renamed[:_INTERFACE_MAX_PER_KIND]:
            lines.append(
                f"    - renamed: `{rename.old_name}` → `{rename.new_name}` "
                f"({_symbol_label(rename.kind, rename.parent)})"
            )
        lines.extend(_overflow_line(len(delta.renamed)))
        for removed in delta.removed[:_INTERFACE_MAX_PER_KIND]:
            lines.append(
                f"    - removed: `{removed.name}` ({_symbol_label(removed.kind, removed.parent)})"
            )
        lines.extend(_overflow_line(len(delta.removed)))
        for changed in delta.changed[:_INTERFACE_MAX_PER_KIND]:
            # Same name, different shape: a rename hunt finds nothing here, and
            # every existing call site of this symbol is now suspect.
            lines.append(
                f"    - changed: `{changed.name}{changed.old_signature or ''}` → "
                f"`{changed.name}{changed.new_signature or ''}` "
                f"({_symbol_label(changed.kind, changed.parent)})"
            )
        lines.extend(_overflow_line(len(delta.changed)))
        for added in delta.added[:_INTERFACE_MAX_PER_KIND]:
            lines.append(f"    - added: `{added.name}` ({_symbol_label(added.kind, added.parent)})")
        lines.extend(_overflow_line(len(delta.added)))
    if len(deltas) > _INTERFACE_MAX_FILES:
        lines.append(f"  - … and {len(deltas) - _INTERFACE_MAX_FILES} more file(s)")
    if report.affected_importers:
        # The actionable half: "someone still imports what you just changed".
        shown = ", ".join(f"`{path}`" for path in report.affected_importers[:10])
        more = len(report.affected_importers) - 10
        lines.append(
            f"  - affected importers (still reference a changed symbol): {shown}"
            + (f" … and {more} more" if more > 0 else "")
        )
    for verdict in report.merge_reverify[:5]:
        lines.append(f"  - re-verify {verdict}")
    return lines


def _overflow_line(count: int) -> list[str]:
    """``… and N more`` for one capping bucket, or nothing when it fits."""
    if count <= _INTERFACE_MAX_PER_KIND:
        return []
    return [f"    - … and {count - _INTERFACE_MAX_PER_KIND} more"]
