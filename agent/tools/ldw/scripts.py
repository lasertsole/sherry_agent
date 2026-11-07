"""Find, read, and write LDW replay scripts under an owning skill."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from loguru import logger

from config import AUTO_SKILLS_DIR
from config.features import LDW

_PLAN_KEY_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _valid_plan_key(plan_key: str) -> bool:
    return bool(plan_key) and all(ch in _PLAN_KEY_CHARS for ch in plan_key)


def ldw_filename(plan_key: str) -> str | None:
    if not _valid_plan_key(plan_key):
        return None
    return f"ldw__{plan_key}.py"


def find_ldw_script(plan_key: str, *, root: Path | None = None) -> Path | None:
    filename = ldw_filename(plan_key)
    if filename is None:
        return None
    base = Path(root or AUTO_SKILLS_DIR)
    try:
        base_resolved = base.resolve()
        matches = []
        for path in base.glob(f"**/scripts/{filename}"):
            if path.is_symlink() or not path.is_file():
                continue
            resolved = path.resolve()
            resolved.relative_to(base_resolved)
            matches.append(resolved)
    except (OSError, ValueError):
        return None
    if len(matches) != 1:
        if len(matches) > 1:
            logger.warning("LDW replay: ambiguous script for {}: {}", plan_key, matches)
        return None
    return matches[0]


def _owning_skill(plan_key: str, *, root: Path | None = None) -> Path | None:
    """Prefer the skill that already owns a PTC script for this plan."""
    base = Path(root or AUTO_SKILLS_DIR)
    try:
        for path in base.glob(f"**/scripts/ptc__{plan_key}__*.py"):
            if path.is_file() and not path.is_symlink():
                return path.parent.parent
    except OSError:
        pass
    return None


def write_ldw_script(
    plan_key: str,
    workflow_meta: dict[str, Any],
    *,
    root: Path | None = None,
) -> Path | None:
    """Write the workflow metadata as Python module constants into an owning/auto skill."""
    filename = ldw_filename(plan_key)
    if filename is None:
        return None
    base = Path(root or AUTO_SKILLS_DIR)
    skill_dir = _owning_skill(plan_key, root=base) or (base / "ldw-replay")
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        skill_md.write_text(
            "---\nname: ldw-replay\ndescription: Compiled LDW replay workflows.\n"
            "when_to_use: When a previously verified plan is replayed through LDW.\n---\n"
            "# LDW replay\n",
            encoding="utf-8",
        )
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    target = scripts / filename
    # The script is Python, not JSON: ``json.dumps`` emits lowercase
    # true/false/null, which raises NameError at import (a schema containing
    # ``additionalProperties: false`` was enough to make every compiled script
    # unloadable). ``repr`` renders valid Python literals and a file that
    # ``exec_module`` can actually read back.
    payload = (
        f"PLAN_KEY = {plan_key!r}\n"
        f"REQUIRED_AGENT_TYPES = {sorted({s['agent_type'] for s in workflow_meta.get('steps', [])})!r}\n"
        f"WORKFLOW_META = {workflow_meta!r}\n"
    )
    if len(payload.encode("utf-8")) > int(LDW["script_max_bytes"]):
        logger.warning("LDW replay: workflow script exceeds size cap for {}", plan_key)
        return None
    target.write_text(payload, encoding="utf-8")
    return target


def load_ldw_script(plan_key: str, *, root: Path | None = None) -> dict[str, Any] | None:
    path = find_ldw_script(plan_key, root=root)
    if path is None:
        return None
    try:
        spec = importlib.util.spec_from_file_location(f"ldw_skill_{plan_key}", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        meta = getattr(module, "WORKFLOW_META", None)
        if isinstance(meta, dict) and str(meta.get("plan_key") or "") == plan_key:
            return meta
    except Exception as exc:  # noqa: BLE001 - a bad skill script is fail-open
        logger.warning("LDW replay: cannot load {}: {}", path, exc)
    return None
