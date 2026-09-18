import json
import shutil
from typing import Any
from pathlib import Path
from datetime import datetime, UTC
from loguru import logger

from context_engine.curator.constants import (
    USAGE_DIR,
    PINNED_FILE,
    STATE_ACTIVE,
    STATE_ARCHIVED,
    ABSORBED_INTO_FILE,
)
from context_engine.curator.helpers import (
    _ensure_dir,
    _read_skill_description,
    _skill_dir,
    is_agent_created,
)


def _skill_record_path(name: str) -> Path:
    return USAGE_DIR / f"{name}.json"


def _default_record(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "state": STATE_ACTIVE,
        "pinned": False,
        "use_count": 0,
        "view_count": 0,
        "patch_count": 0,
        "activity_count": 0,
        "created_at": datetime.now(UTC).isoformat(),
        "last_activity_at": None,
    }


def load_record(name: str) -> dict[str, Any]:
    path = _skill_record_path(name)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):  # noqa: S110
            pass
    return _default_record(name)


def save_record(name: str, data: dict[str, Any]) -> None:
    _ensure_dir(USAGE_DIR)
    _skill_record_path(name).write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def seed_record_if_missing(name: str) -> bool:
    rec = load_record(name)
    if rec.get("_persisted"):
        return False
    rec["_persisted"] = True
    save_record(name, rec)
    return True


def set_state(name: str, state: str) -> None:
    err = _pinned_guard(name)
    if err:
        logger.warning(err)
        return
    rec = load_record(name)
    rec["state"] = state
    rec["_persisted"] = True
    save_record(name, rec)


def is_pinned(name: str) -> bool:
    rec = load_record(name)
    if rec.get("pinned"):
        return True
    sd = _skill_dir(name)
    return sd is not None and (sd / PINNED_FILE).exists()


def _pinned_guard(name: str) -> str | None:
    if is_pinned(name):
        return (
            f"Skill '{name}' is pinned and cannot be deleted or archived. "
            f"Unpin it first if you want to change it."
        )
    return None


def pin_skill(name: str) -> tuple[bool, str]:
    """Pin a skill so the curator never merges or removes it.

    Pinning is recorded in the skill's usage record (``pinned: True``), which
    ``is_pinned`` / ``_pinned_guard`` both honor. The skill must already exist
    on disk under ``skills/auto/``.
    """
    sd = _skill_dir(name)
    if sd is None:
        return False, f"Skill directory not found: {name}"
    rec = load_record(name)
    rec["pinned"] = True
    rec["_persisted"] = True
    save_record(name, rec)
    logger.info(f"Curator pinned skill: {name}")
    return True, f"Pinned {name}"


def unpin_skill(name: str) -> tuple[bool, str]:
    """Unpin a skill, allowing the curator to merge or remove it again.

    Clears the ``pinned`` flag in the skill's usage record (if present) and
    removes any ``.pinned`` marker file inside the skill directory. Nested
    skills under ``skills/auto/<category>/<skill>/`` are resolved via
    ``_skill_dir``, mirroring ``is_pinned``.
    """
    sd = _skill_dir(name)
    if sd is None:
        return False, f"Skill directory not found: {name}"
    rec = load_record(name)
    if rec.get("pinned") or rec.get("_persisted"):
        rec["pinned"] = False
        rec["_persisted"] = True
        save_record(name, rec)
    marker = sd / PINNED_FILE
    if marker.exists():
        try:
            marker.unlink()
        except Exception as e:
            return False, f"Failed to remove .pinned marker: {e}"
    logger.info(f"Curator unpinned skill: {name}")
    return True, f"Unpinned {name}"


def delete_skill(name: str, absorbed_into: str = "") -> tuple[bool, str]:
    err = _pinned_guard(name)
    if err:
        return False, err
    sd = _skill_dir(name)
    if sd is None:
        return False, f"Skill directory not found: {name}"
    try:
        shutil.rmtree(str(sd))
    except Exception as e:
        return False, f"Failed to delete skill: {e}"
    rec_path = _skill_record_path(name)
    if rec_path.exists():
        try:
            rec_path.unlink()
        except Exception as e:
            logger.debug("Failed to remove usage record for {}: {}", name, e)
    return True, f"Deleted {name}" + (f" (absorbed into {absorbed_into})" if absorbed_into else "")


def agent_created_report() -> list[dict[str, Any]]:
    from context_engine.curator.constants import AUTO_SKILLS_DIR

    rows: list[dict[str, Any]] = []
    if not AUTO_SKILLS_DIR.exists():
        return rows
    # Walk recursively: skills live at depth 2 (skills/auto/<category>/<skill>/SKILL.md).
    # A flat iterdir() only sees category dirs and misses every nested skill.
    for skill_md in sorted(
        AUTO_SKILLS_DIR.glob("**/SKILL.md"), key=lambda p: p.parent.name.lower()
    ):
        entry = skill_md.parent
        if entry.name.startswith("."):
            continue
        name = entry.name
        rec = load_record(name)
        rec["name"] = name
        rec["description"] = _read_skill_description(entry)
        rec["pinned"] = is_pinned(name)
        rec["_persisted"] = rec.get("_persisted", False)
        rows.append(rec)
    _cleanup_orphan_records({r["name"] for r in rows})
    _cleanup_orphan_dirs()
    return rows


def _iter_empty_dirs(root: Path) -> list[Path]:
    """Collect directories that contain no files recursively, bottom-up.

    A directory is “empty” when no file exists anywhere beneath it. ``.usage``
    and any dot-prefixed dirs are hidden config and always excluded. Returns
    child-most empty dirs first so parents are post-processed safely.
    """
    empty: list[Path] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return empty
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        empty.extend(_iter_empty_dirs(child))
    # A dir with no regular files at all (even after recursing) is empty.
    if not any(p.is_file() for p in root.iterdir() if not p.name.startswith(".")):
        empty.append(root)
    return empty


def _cleanup_orphan_dirs() -> None:
    """Remove orphaned empty directories under ``skills/auto/``.

    Complements ``_cleanup_orphan_records``: that removes usage JSONs pointing
    at missing skills; this removes the reverse — placeholder/leftover skill
    dirs that contain no ``SKILL.md`` and not even any file (e.g. ``media``,
    ``multimodal``). Dot-dirs (``.usage``) and anything with content are never
    touched. Runs on a best-effort basis; a stale/malformed tree may prevent
    deletion, which is safer than over-removal.
    """
    from context_engine.curator.constants import AUTO_SKILLS_DIR

    if not AUTO_SKILLS_DIR.exists():
        return
    for d in _iter_empty_dirs(AUTO_SKILLS_DIR):
        try:
            d.rmdir()
            logger.debug("Curator removed orphan empty dir: {}", d.name)
        except OSError:  # noqa: S110
            pass


def _cleanup_orphan_records(live_names: set[str]) -> None:
    if not USAGE_DIR.exists():
        return
    for f in USAGE_DIR.iterdir():
        if f.suffix != ".json":
            continue
        if f.stem in live_names:
            continue
        # Archived skills have no live directory by design — their record is
        # kept so restore can replay the skill's history. Not an orphan.
        if load_record(f.stem).get("state") == STATE_ARCHIVED:
            continue
        try:
            f.unlink()
            logger.debug("Curator removed orphan usage record: {}", f.name)
        except Exception as e:
            logger.debug("Curator failed to remove orphan usage record {}: {}", f.name, e)


def _archive_dir() -> Path:
    from context_engine.curator.constants import ARCHIVE_DIR

    return ARCHIVE_DIR


def archive_skill(name: str, absorbed_into: str = "") -> tuple[bool, str]:
    """Move an agent-created skill directory into ``skills/.archive/``.

    The recoverable counterpart of ``delete_skill``: the directory is moved
    (never removed), and the usage record is kept with ``state="archived"`` so
    restore replays the same history. Pinned and bundled/hub skills are
    refused. A non-empty ``absorbed_into`` records the umbrella that absorbed
    this skill's content as an ``ABSORBED_INTO`` marker inside the archived
    directory, so the relation survives a restore. Returns ``(ok, message)``.
    """
    err = _pinned_guard(name)
    if err:
        return False, err
    absorbed_into = absorbed_into.strip()
    sd = _skill_dir(name)
    if sd is None:
        return False, f"Skill directory not found: {name}"
    if not is_agent_created(name, sd):
        return False, f"Skill '{name}' is bundled or hub-installed; never archive"

    archive_root = _archive_dir()
    try:
        archive_root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"Failed to create archive dir: {e}"

    # Flatten category nesting into ".archive/<skill>/" so restores are simple.
    # A collision gets a timestamp suffix, which the agent-side restore finds
    # via its "<skill>-" prefix search.
    dest = archive_root / sd.name
    if dest.exists():
        dest = archive_root / f"{sd.name}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"

    try:
        sd.rename(dest)
    except OSError:
        # Cross-device — fall back to shutil.move
        try:
            shutil.move(str(sd), str(dest))
        except Exception as e:
            return False, f"Failed to archive skill: {e}"

    if absorbed_into:
        try:
            (dest / ABSORBED_INTO_FILE).write_text(absorbed_into + "\n", encoding="utf-8")
        except OSError as e:
            logger.warning("Curator archived '{}' but failed to record absorbed_into: {}", name, e)

    # Rename first, state second: a failed move never leaves a record claiming
    # "archived".
    set_state(name, STATE_ARCHIVED)
    logger.info(f"Curator archived skill: {name} -> {dest}")
    suffix = f" (absorbed into {absorbed_into})" if absorbed_into else ""
    return True, f"Archived {name} to {dest}{suffix}"
