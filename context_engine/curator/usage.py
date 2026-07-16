import json
import shutil
from typing import Any
from pathlib import Path
from datetime import datetime, timezone
from loguru import logger

from context_engine.curator.constants import (
    ARCHIVE_DIR,
    USAGE_DIR,
    PINNED_FILE,
    STATE_ACTIVE,
    STATE_ARCHIVED,
)
from context_engine.curator.helpers import _ensure_dir, _builtin_skill_names


def _skill_record_path(name: str) -> Path:
    return USAGE_DIR / f"{name}.json"


def _skill_dir(name: str) -> Path | None:
    from context_engine.curator.constants import AUTO_SKILLS_DIR
    candidate = AUTO_SKILLS_DIR / name
    if candidate.is_dir() and (candidate / "SKILL.md").exists():
        return candidate
    return None


def _default_record(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "state": STATE_ACTIVE,
        "pinned": False,
        "use_count": 0,
        "view_count": 0,
        "patch_count": 0,
        "activity_count": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_activity_at": None,
    }


def load_record(name: str) -> dict[str, Any]:
    path = _skill_record_path(name)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
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


def bump_usage(name: str, field: str = "use_count") -> None:
    if name in _builtin_skill_names():
        return
    rec = load_record(name)
    rec[field] = int(rec.get(field, 0)) + 1
    rec["activity_count"] = int(rec.get("activity_count", 0)) + 1
    rec["last_activity_at"] = datetime.now(timezone.utc).isoformat()
    rec["_persisted"] = True
    save_record(name, rec)


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


def _background_review_write_guard(name: str, action: str) -> str | None:
    if is_pinned(name):
        return (
            f"Refusing background curator {action} for pinned skill "
            f"'{name}': pinned skills are off-limits to autonomous "
            f"maintenance. Unpin it first if you want it changed."
        )
    return None


def pin_skill(name: str) -> None:
    rec = load_record(name)
    rec["pinned"] = True
    save_record(name, rec)
    sd = _skill_dir(name)
    if sd:
        (sd / PINNED_FILE).touch()


def unpin_skill(name: str) -> None:
    rec = load_record(name)
    rec["pinned"] = False
    save_record(name, rec)
    sd = _skill_dir(name)
    if sd:
        p = sd / PINNED_FILE
        if p.exists():
            p.unlink()


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
        except Exception:
            pass
    return True, f"Deleted {name}" + (f" (absorbed into {absorbed_into})" if absorbed_into else "")


def _remove_skill(name: str, absorbed_into: str = "") -> tuple[bool, str]:
    return archive_skill(name, absorbed_into=absorbed_into)


def archive_skill(name: str, absorbed_into: str = "") -> tuple[bool, str]:
    err = _pinned_guard(name)
    if err:
        return False, err
    sd = _skill_dir(name)
    if sd is None:
        return False, f"Skill directory not found: {name}"
    _ensure_dir(ARCHIVE_DIR)
    dest = ARCHIVE_DIR / name
    if dest.exists():
        return False, f"Archive destination already exists: {name}"
    try:
        shutil.move(str(sd), str(dest))
    except Exception as e:
        return False, f"Failed to move skill to archive: {e}"
    rec = load_record(name)
    rec["state"] = STATE_ARCHIVED
    rec["archived_at"] = datetime.now(timezone.utc).isoformat()
    if absorbed_into:
        rec["absorbed_into"] = absorbed_into
    rec["_persisted"] = True
    save_record(name, rec)
    return True, f"Archived {name}" + (f" (absorbed into {absorbed_into})" if absorbed_into else "")


def restore_skill(name: str) -> tuple[bool, str]:
    from context_engine.curator.constants import AUTO_SKILLS_DIR
    src = ARCHIVE_DIR / name
    if not src.exists():
        return False, f"Archived skill not found: {name}"
    _ensure_dir(AUTO_SKILLS_DIR)
    dest = AUTO_SKILLS_DIR / name
    if dest.exists():
        return False, f"Skill already exists at destination: {name}"
    try:
        shutil.move(str(src), str(dest))
    except Exception as e:
        return False, f"Failed to restore skill: {e}"
    rec = load_record(name)
    rec["state"] = STATE_ACTIVE
    rec.pop("archived_at", None)
    rec.pop("absorbed_into", None)
    rec["last_activity_at"] = datetime.now(timezone.utc).isoformat()
    rec["_persisted"] = True
    save_record(name, rec)
    return True, f"Restored {name}"


def list_archived() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not ARCHIVE_DIR.exists():
        return result
    for entry in sorted(ARCHIVE_DIR.iterdir()):
        if entry.is_dir() and (entry / "SKILL.md").exists():
            rec = load_record(entry.name)
            rec["name"] = entry.name
            result.append(rec)
    return result


def agent_created_report() -> list[dict[str, Any]]:
    from context_engine.curator.constants import AUTO_SKILLS_DIR
    rows: list[dict[str, Any]] = []
    if not AUTO_SKILLS_DIR.exists():
        return rows
    for entry in sorted(AUTO_SKILLS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not (entry / "SKILL.md").exists():
            continue
        rec = load_record(entry.name)
        rec["name"] = entry.name
        rec["pinned"] = is_pinned(entry.name)
        rec["_persisted"] = rec.get("_persisted", False)
        rows.append(rec)
    return rows
