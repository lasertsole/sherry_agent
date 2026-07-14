import json
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Optional, Set
from loguru import logger

from skills.curator.constants import SKILLS_DIR


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
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


def _atomic_json_write(path: Path, data: Dict[str, Any], indent: int = 2) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=indent, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _builtin_skill_names() -> Set[str]:
    builtin_dir = SKILLS_DIR / "builtin"
    names: Set[str] = set()
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


def _cron_referenced_skills() -> Set[str]:
    try:
        from skills.builtin.core.cron.scripts.base import cron_service
        store = cron_service._load_store()
        names: Set[str] = set()
        for job in store.jobs:
            if job.payload and job.payload.message:
                for token in job.payload.message.split():
                    if token.startswith("skill:"):
                        names.add(token[6:])
        return names
    except Exception as e:
        logger.debug("Curator could not read cron skill references: {}", e)
        return set()


def _needle_in_path_component(needle: str, path: str) -> bool:
    norm_needle = needle.replace("-", "_")
    for part in path.replace("\\", "/").split("/"):
        if not part:
            continue
        stem = part.rsplit(".", 1)[0] if "." in part else part
        if stem.replace("-", "_") == norm_needle:
            return True
    return False
