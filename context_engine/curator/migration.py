"""Curator consolidation file migration and archive phases.

Split out of ``orchestrator.py``; the orchestrator re-imports these names so the
test-pinned private surface (``orchestrator._write_supporting_files`` /
``_migrate_source_files`` / ``_archive_consolidated_sources`` /
``_archive_pruned_skills``) stays unchanged.
"""

from loguru import logger

from context_engine.curator.helpers import _skill_dir as _resolve_skill_dir
from context_engine.curator.refresh import _resolve_skill_writer
from context_engine.curator.usage import archive_skill


def _collect_umbrella_names(consolidations: list) -> set[str]:
    umbrella_names = set()
    for entry in consolidations:
        into = entry.get("into", "").strip()
        if into:
            umbrella_names.add(into)
    return umbrella_names


def _read_source_blocks(merged_skills: list) -> list[str]:
    source_blocks: list[str] = []
    for entry in merged_skills:
        src_name = entry.get("from", "").strip()
        src_dir = _resolve_skill_dir(src_name)
        src_md = src_dir / "SKILL.md" if src_dir is not None else None
        if src_md and src_md.exists():
            src_text = src_md.read_text(encoding="utf-8")
            source_blocks.append(f"### {src_name}\n\n{src_text}")
        else:
            source_blocks.append(f"### {src_name}\n\n{entry.get('reason', '')}")
    return source_blocks


def _collect_file_inventory(merged_skills: list) -> str:
    file_inventory_lines: list[str] = []
    for entry in merged_skills:
        src_name = entry.get("from", "").strip()
        src_dir = _resolve_skill_dir(src_name)
        if src_dir is None:
            continue
        for subdir in ("references", "templates", "scripts", "assets"):
            src_sub = src_dir / subdir
            if not src_sub.is_dir():
                continue
            for f in src_sub.iterdir():
                if f.is_file():
                    file_inventory_lines.append(f"- {subdir}/{f.name} (from {src_name})")
    return "\n".join(file_inventory_lines)


def _write_supporting_files(umbrella: str, supporting_files: dict[str, str]) -> None:
    writer = _resolve_skill_writer("write_file")
    if writer is None:
        return

    for file_path, file_content in supporting_files.items():
        wr = writer.write_file(umbrella, file_path, file_content)
        if wr.get("success"):
            logger.debug("Curator wrote umbrella support file {}/{}", umbrella, file_path)
        else:
            logger.warning(
                "Curator failed to write {}/{}: {}", umbrella, file_path, wr.get("error")
            )


def _migrate_source_files(umbrella: str, merged_skills: list, written: set[str]) -> None:
    writer = _resolve_skill_writer("write_file")
    if writer is None:
        return

    for entry in merged_skills:
        src_name = entry.get("from", "").strip()
        src_dir = _resolve_skill_dir(src_name)
        if src_dir is None:
            continue
        for subdir in ("references", "templates", "scripts", "assets", "examples", "resources"):
            src_sub = src_dir / subdir
            if not src_sub.is_dir():
                continue
            for f in src_sub.iterdir():
                if not f.is_file():
                    continue
                file_path = f"{subdir}/{f.name}"
                if file_path in written:
                    logger.debug(
                        "Curator: skip migrating {}/{} (umbrella support file already written)",
                        src_name,
                        file_path,
                    )
                    continue
                file_content = f.read_text(encoding="utf-8")
                wr = writer.write_file(umbrella, file_path, file_content)
                if wr.get("success"):
                    logger.debug(
                        "Curator migrated {}/{} -> {}/{}", src_name, f.name, umbrella, f.name
                    )
                else:
                    logger.warning(
                        "Curator failed to migrate {}/{}: {}", src_name, f.name, wr.get("error")
                    )


def _archive_consolidated_sources(consolidations: list) -> None:
    for entry in consolidations:
        name = entry.get("from", "").strip()
        into = entry.get("into", "").strip()
        if not name or not into:
            continue
        if _resolve_skill_dir(into) is None:
            logger.warning("Curator skipped archiving '{}': umbrella '{}' not found", name, into)
            continue
        ok, msg = archive_skill(name, absorbed_into=into)
        if ok:
            logger.info("Curator archived '{}' into umbrella '{}': {}", name, into, msg)
        else:
            logger.warning("Curator failed to archive '{}' into umbrella '{}': {}", name, into, msg)


def _archive_pruned_skills(prunings: list, consolidations: list) -> None:
    for entry in prunings:
        name = entry.get("name", "").strip()
        if not name:
            continue
        in_consolidation = any(e.get("from", "").strip() == name for e in consolidations)
        if in_consolidation:
            continue
        ok, msg = archive_skill(name)
        if ok:
            logger.info("Curator pruned '{}' (archived): {}", name, msg)
        else:
            logger.warning("Curator failed to prune '{}': {}", name, msg)
