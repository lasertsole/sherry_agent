import json
from typing import Any
from pathlib import Path
from datetime import datetime
from loguru import logger


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
    except OSError:  # noqa: S110
        pass


def _atomic_json_write(path: Path, data: dict[str, Any], indent: int = 2) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, indent=indent, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def _read_skill_description(skill_dir: Path) -> str:
    md = skill_dir / "SKILL.md"
    if not md.exists():
        return ""
    try:
        text = md.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return ""
        parts = text.split("---", 2)
        if len(parts) < 3:
            return ""
        import yaml

        meta = yaml.safe_load(parts[1]) or {}
        return str(meta.get("description", ""))
    except Exception:
        return ""


def _needle_in_path_component(needle: str, path: str) -> bool:
    norm_needle = needle.replace("-", "_")
    for part in path.replace("\\", "/").split("/"):
        if not part:
            continue
        stem = part.rsplit(".", 1)[0] if "." in part else part
        if stem.replace("-", "_") == norm_needle:
            return True
    return False


def _skill_dir(name: str) -> Path | None:
    """Resolve a skill directory by its leaf name, recursing into category subdirs.

    Skills under ``skills/auto/`` use a two-level layout::

        skills/auto/<category>/<skill>/SKILL.md

    The name is the leaf dir (e.g. ``docker``).  A flat ``AUTO_SKILLS_DIR / name``
    lookup misses nested skills, so we walk ``**/SKILL.md`` and match by parent dir
    name (mirrors ``skill_manage._find_skill``).

    Canonical implementation (audit 3.1.3): ``curator.usage._skill_dir`` and
    ``curator.orchestrator._resolve_skill_dir`` are aliases of this function.
    """
    from context_engine.curator.constants import AUTO_SKILLS_DIR

    candidate = AUTO_SKILLS_DIR / name
    if candidate.is_dir() and (candidate / "SKILL.md").exists():
        return candidate
    if not AUTO_SKILLS_DIR.exists():
        return None
    for skill_md in AUTO_SKILLS_DIR.glob("**/SKILL.md"):
        if skill_md.parent.name == name:
            return skill_md.parent
    return None


def _read_skill_name(skill_md: Path, fallback: str) -> str:
    """Parse the ``name:`` field from a SKILL.md YAML frontmatter."""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return fallback
    in_frontmatter = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped == "---":
            if in_frontmatter:
                break
            in_frontmatter = True
            continue
        if in_frontmatter and stripped.startswith("name:"):
            value = stripped.split(":", 1)[1].strip().strip("\"'")
            if value:
                return value
    return fallback


def _bundled_or_hub_names() -> set[str]:
    """Names maintained upstream (bundled seed manifest / skills-hub lock).

    Mirrors ``agent.tools.pub_base.skill_usage`` so the curator can refuse
    non-agent-created skills without importing the agent layer.
    """
    from context_engine.curator.constants import AUTO_SKILLS_DIR

    names: set[str] = set()

    manifest = AUTO_SKILLS_DIR / ".bundled_manifest"
    if manifest.exists():
        try:
            for line in manifest.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                name = line.split(":", 1)[0].strip()
                if name:
                    names.add(name)
        except OSError as e:
            logger.debug("Curator failed to read bundled manifest: {}", e)

    lock = AUTO_SKILLS_DIR / ".hub" / "lock.json"
    if lock.exists():
        data: Any = None
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("Curator failed to read hub lock file: {}", e)
        installed = data.get("installed") if isinstance(data, dict) else None
        if isinstance(installed, dict):
            names.update(str(k) for k in installed)
            for entry in installed.values():
                if not isinstance(entry, dict):
                    continue
                install_path = entry.get("install_path")
                if not isinstance(install_path, str) or not install_path.strip():
                    continue
                skill_dir = Path(install_path)
                if not skill_dir.is_absolute():
                    skill_dir = AUTO_SKILLS_DIR / skill_dir
                skill_md = skill_dir / "SKILL.md"
                if skill_md.exists():
                    names.add(_read_skill_name(skill_md, skill_md.parent.name))

    return names


def is_agent_created(name: str, skill_dir: Path | None = None) -> bool:
    """Whether *name* (or the skill at *skill_dir*) is curator-eligible.

    Only agent-authored skills are eligible; bundled seed skills and
    hub-installed skills are maintained by their upstream sources.
    """
    off_limits = _bundled_or_hub_names()
    if name in off_limits:
        return False
    if skill_dir is not None:
        frontmatter_name = _read_skill_name(skill_dir / "SKILL.md", skill_dir.name)
        if frontmatter_name in off_limits:
            return False
    return True
