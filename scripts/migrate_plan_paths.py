"""One-shot migration of plan files from ``.omo/plans/`` into the session tree.

Plans now live in ``workspace/sessions/<session_id>/plans/`` (session-scoped:
``clear_session`` deletes that tree, plans included). This script moves every
``.omo/plans/*.md`` referenced by ``.omo/boulder.json`` and rewrites the
``active_plan`` pointers — top-level and every ``works[*]`` entry.

The session id is derived from the referencing boulder work, in order:
``session_ids[0]`` → the first key of ``session_origins`` → the first
``session_id`` in ``task_sessions``. When none exists the script reports an
error and refuses to guess. Plan files that no boulder entry references are
reported as ``skip`` and left in place: there is no session association to
derive and guessing is forbidden.

Dry-run by default; ``--apply`` performs the moves and the boulder rewrite.
A pass is atomic — when any error is reported, nothing is written or moved.
Re-running is safe: an already-migrated pointer is skipped and a destination
whose bytes match the source is treated as done.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Allow direct execution (`python scripts/migrate_plan_paths.py`) from any cwd.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.path import ROOT_DIR, SESSIONS_DIR, is_safe_session_segment  # noqa: E402

_OMO_PLANS_REL = Path(".omo") / "plans"
_BOULDER_REL = Path(".omo") / "boulder.json"
_SESSIONS_REL = SESSIONS_DIR.relative_to(ROOT_DIR)


@dataclass
class MigrationReport:
    """Outcome of one dry-run/apply pass (the pass itself writes nothing)."""

    moves: list[tuple[str, str]] = field(default_factory=list)
    already: list[tuple[str, str]] = field(default_factory=list)
    boulder_changes: list[tuple[str, str, str]] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def derive_session_id(work: dict[str, Any]) -> str | None:
    """Derive a work's session id: session_ids → session_origins → task_sessions."""
    session_ids = work.get("session_ids") or []
    if session_ids:
        return str(session_ids[0])
    origins = work.get("session_origins") or {}
    if origins:
        return str(next(iter(origins)))
    tasks = work.get("task_sessions") or {}
    for entry in tasks.values():
        if isinstance(entry, dict) and entry.get("session_id"):
            return str(entry["session_id"])
    return None


def _omo_plan_filename(plan_ref: str) -> str | None:
    """Return the filename when *plan_ref* points into ``.omo/plans/``, else None."""
    if not plan_ref:
        return None
    ref = Path(plan_ref)
    return ref.name if ref.parent.as_posix() == _OMO_PLANS_REL.as_posix() else None


def _destination(root_dir: Path, session_id: str, filename: str) -> Path:
    return root_dir / _SESSIONS_REL / session_id / "plans" / filename


def _enqueue(
    root_dir: Path,
    report: MigrationReport,
    label: str,
    plan_ref: str,
    work: dict[str, Any],
    targets: dict[str, str],
    source_targets: dict[str, str],
    jobs: list[tuple[Path, Path]],
) -> None:
    """Validate one ``active_plan`` pointer and stage its move + rewrite."""
    filename = _omo_plan_filename(plan_ref)
    if filename is None:
        return
    session_id = derive_session_id(work)
    if session_id is None:
        report.errors.append(f"{label}: no session reference for {plan_ref!r}; refusing to guess")
        return
    if not is_safe_session_segment(session_id):
        report.errors.append(f"{label}: unsafe session id {session_id!r}")
        return

    source = root_dir / plan_ref
    dest = _destination(root_dir, session_id, filename)
    dest_rel = dest.relative_to(root_dir).as_posix()
    source_key = source.as_posix()
    previous = source_targets.get(source_key)
    if previous is not None and previous != dest_rel:
        report.errors.append(
            f"{label}: {plan_ref!r} is also targeted at {previous!r} by another session"
        )
        return
    if dest.is_file() and source.is_file() and dest.read_bytes() != source.read_bytes():
        report.errors.append(f"{label}: destination differs: {dest}")
        return
    if not source.is_file() and not dest.is_file():
        report.errors.append(f"{label}: plan source missing: {source}")
        return

    source_targets.setdefault(source_key, dest_rel)
    targets.setdefault(filename, dest_rel)
    if dest.is_file():
        report.already.append((source_key, dest.as_posix()))
    elif (source, dest) not in jobs:
        jobs.append((source, dest))
    work["active_plan"] = dest_rel
    report.boulder_changes.append((label, plan_ref, dest_rel))


def migrate(root_dir: Path, *, apply: bool) -> MigrationReport:
    """Run one migration pass against *root_dir* (see the module docstring)."""
    report = MigrationReport()
    boulder_path = root_dir / _BOULDER_REL
    try:
        boulder: dict[str, Any] = json.loads(boulder_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report.errors.append(f"cannot read {boulder_path}: {exc}")
        return report

    works_raw = boulder.get("works")
    works: dict[str, Any] = works_raw if isinstance(works_raw, dict) else {}
    targets: dict[str, str] = {}
    source_targets: dict[str, str] = {}
    jobs: list[tuple[Path, Path]] = []

    for work_id, work in works.items():
        if isinstance(work, dict):
            _enqueue(
                root_dir,
                report,
                f"works.{work_id}.active_plan",
                str(work.get("active_plan") or ""),
                work,
                targets,
                source_targets,
                jobs,
            )

    top_ref = str(boulder.get("active_plan") or "")
    top_filename = _omo_plan_filename(top_ref)
    if top_filename is not None:
        if top_filename in targets:
            boulder["active_plan"] = targets[top_filename]
            report.boulder_changes.append(("active_plan", top_ref, targets[top_filename]))
        else:
            _enqueue(
                root_dir,
                report,
                "active_plan",
                top_ref,
                boulder,
                targets,
                source_targets,
                jobs,
            )

    plans_dir = root_dir / _OMO_PLANS_REL
    if plans_dir.is_dir():
        for plan in sorted(plans_dir.glob("*.md")):
            if plan.as_posix() not in source_targets:
                report.unmapped.append(plan.relative_to(root_dir).as_posix())

    report.moves = [(source.as_posix(), dest.as_posix()) for source, dest in jobs]
    if report.errors or not apply:
        return report

    for source, dest in jobs:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(dest))
    boulder_path.write_text(
        json.dumps(boulder, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def _emit(report: MigrationReport, *, apply: bool) -> None:
    """Print the pass report to stdout (``sys.stdout.write`` avoids the T20 gate)."""
    out = sys.stdout.write
    out(f"Mode: {'APPLY' if apply else 'DRY-RUN'}\n")
    for label, old, new in report.boulder_changes:
        out(f"[boulder] {label}: {old} -> {new}\n")
    for old, new in report.moves:
        out(f"[{'move' if apply else 'would move'}] {old} -> {new}\n")
    for old, new in report.already:
        out(f"[already migrated] {old} == {new}\n")
    for plan in report.unmapped:
        out(f"[skip] {plan}: no boulder work references it; refusing to guess a session_id\n")
    for error in report.errors:
        out(f"[error] {error}\n")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: dry-run by default, ``--apply`` to write."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--root", type=Path, default=ROOT_DIR, help="repo root to migrate")
    args = parser.parse_args(argv)

    report = migrate(args.root, apply=args.apply)
    _emit(report, apply=args.apply)
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
