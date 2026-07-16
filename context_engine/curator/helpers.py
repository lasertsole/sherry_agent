import json
from typing import Any
from pathlib import Path
from loguru import logger
from datetime import datetime


from context_engine.curator.constants import SKILLS_DIR


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _ensure_dir(p: Path) -> None:
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _atomic_json_write(path: Path, data: dict[str, Any], indent: int = 2) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=indent, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _builtin_skill_names() -> set[str]:
    builtin_dir = SKILLS_DIR / "builtin"
    names: set[str] = set()
    if not builtin_dir.exists():
        return names
    for skill_file in builtin_dir.rglob("SKILL.md"):
        try:
            text = skill_file.read_text(encoding="utf-8")
            if text.startswith("---"):
                import yaml
                parts = text.split("---", 2)
                if len(parts) >= 3:
                    meta = yaml.safe_load(parts[1]) or {}
                    n = meta.get("name", skill_file.parent.name)
                    names.add(str(n))
        except Exception:
            names.add(skill_file.parent.name)
    return names


def _needle_in_path_component(needle: str, path: str) -> bool:
    norm_needle = needle.replace("-", "_")
    for part in path.replace("\\", "/").split("/"):
        if not part:
            continue
        stem = part.rsplit(".", 1)[0] if "." in part else part
        if stem.replace("-", "_") == norm_needle:
            return True
    return False
