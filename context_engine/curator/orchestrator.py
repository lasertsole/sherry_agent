"""Curator orchestration: the run entry points and consolidation apply loop.

The heavy lifting lives in focused siblings — ``review`` (LLM pass + candidate
snapshot), ``run_state`` (run accumulator/snapshots/report finalization),
``umbrella`` (umbrella generation), ``migration`` (file migration + archive) and
``refresh`` (provider resolution + system-prompt refresh). This module only
coordinates them, and re-exports the moved names so ``context_engine.curator``
and the test-pinned private surface are unchanged.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from context_engine.curator.config import get_consolidate, get_min_idle_hours
from context_engine.curator.helpers import _skill_dir as _resolve_skill_dir
from context_engine.curator.migration import (
    _archive_consolidated_sources,
    _archive_pruned_skills,
    _collect_file_inventory,
    _collect_umbrella_names,
    _migrate_source_files,
    _read_source_blocks,
    _write_supporting_files,
)
from context_engine.curator.refresh import (
    _provider_misses_logged as _provider_misses_logged,  # noqa: F401  (re-export/patch seam)
    _resolve_skill_writer,
    _schedule_system_prompt_refresh,
)
from context_engine.curator.review import (
    CURATOR_DRY_RUN_BANNER,
    CURATOR_REVIEW_PROMPT,
    _run_llm_consolidation_pass,
    reset_report_cache,
)
from context_engine.curator.run_state import (
    _ReviewRun,
    _append_rename_summary,
    _build_auto_summary,
    _collect_auto_transition_counts,
    _finalize_run,
    _record_intermediate_state,
    _snapshot_agent_skills,
)
from context_engine.curator.transitions import should_run_now
from context_engine.curator.umbrella import _generate_umbrella_skill

__all__ = [
    "CURATOR_DRY_RUN_BANNER",
    "CURATOR_REVIEW_PROMPT",
    "maybe_run_curator",
    "run_curator_review",
]


def run_curator_review(
    on_summary: Callable[[str], None] | None = None,
    dry_run: bool = False,
    consolidate: bool | None = None,
) -> dict[str, Any]:
    if consolidate is None:
        consolidate = get_consolidate()
    reset_report_cache()
    start = datetime.now(UTC)
    counts = _collect_auto_transition_counts(dry_run, start)
    run = _ReviewRun(
        start=start,
        dry_run=dry_run,
        prefix="dry-run auto: " if dry_run else "auto: ",
        counts=counts,
        auto_summary=_build_auto_summary(counts),
    )
    _record_intermediate_state(run)
    run.before_report, run.before_names = _snapshot_agent_skills()

    if not consolidate:
        run.llm_meta = {
            "final": "",
            "summary": "skipped (consolidation off)",
            "model": "",
            "provider": "",
            "tool_calls": [],
            "error": None,
        }
        run.final_summary = f"{run.prefix}{run.auto_summary}; llm: skipped (consolidation off)"
        return _finalize_run(run, on_summary)

    _run_llm_consolidation_pass(run)
    run.final_summary = _append_rename_summary(run)

    if not run.dry_run:
        try:
            _apply_consolidation(run.llm_meta.get("final", ""))
        except Exception as e:
            logger.debug("Curator consolidation apply failed: {}", e)

    return _finalize_run(run, on_summary)


def maybe_run_curator(
    *,
    idle_for_seconds: float | None = None,
    on_summary: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    try:
        if not should_run_now():
            return None
        if idle_for_seconds is not None:
            min_idle_s = get_min_idle_hours() * 3600.0
            if idle_for_seconds < min_idle_s:
                return None
        return run_curator_review(on_summary=on_summary)
    except Exception as e:
        logger.debug("maybe_run_curator failed: {}", e)
        return None


def _merge_umbrella_skills(consolidations: list) -> None:
    from context_engine.curator.usage import seed_record_if_missing

    writer = _resolve_skill_writer("create_skill")
    if writer is None:
        return

    for umbrella in sorted(_collect_umbrella_names(consolidations)):
        skill_dir = _resolve_skill_dir(umbrella)
        if skill_dir is not None:
            continue
        merged_skills = [e for e in consolidations if e.get("into", "").strip() == umbrella]

        source_blocks = _read_source_blocks(merged_skills)
        reasons = [f"- {e.get('from', '?')}: {e.get('reason', '')}" for e in merged_skills]
        merged_content = "\n\n".join(source_blocks)
        file_inventory = _collect_file_inventory(merged_skills)

        umbrella_content, supporting_files = _generate_umbrella_skill(
            umbrella, reasons, merged_content, file_inventory
        )

        if umbrella_content.startswith("---"):
            umbrella_content = umbrella_content + "\n"
        result = writer.create_skill(umbrella, umbrella_content)
        if result.get("success"):
            logger.info("Curator created umbrella skill: {}", umbrella)
        else:
            logger.warning(
                "Curator failed to create umbrella '{}': {}", umbrella, result.get("error")
            )
            continue

        seed_record_if_missing(umbrella)

        # Write supporting files the LLM split out of the main SKILL.md, then
        # migrate any source subdirectory files (skip ones already written).
        written = {p for p in supporting_files}
        _write_supporting_files(umbrella, supporting_files)
        _migrate_source_files(umbrella, merged_skills, written)


def _apply_consolidation(llm_final: str) -> None:
    from context_engine.curator.classify import _parse_structured_summary

    parsed = _parse_structured_summary(llm_final)
    consolidations = parsed.get("consolidations", [])
    prunings = parsed.get("prunings", [])
    if not consolidations and not prunings:
        return
    if _resolve_skill_writer("apply_consolidation") is None:
        # Without a writer, merging would be a no-op while the archive phases
        # below still ran -> source skills archived without their umbrella.
        # Abort the whole apply; the per-method gates keep direct callers safe.
        return

    _merge_umbrella_skills(consolidations)
    _archive_consolidated_sources(consolidations)
    _archive_pruned_skills(prunings, consolidations)
    _schedule_system_prompt_refresh()
