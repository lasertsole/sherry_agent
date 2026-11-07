"""Resolve a pre-compiled PTC replay script under a skill's ``scripts/`` root.

A script is named ``ptc__<plan_key>__<step_id>.py`` and lives at the TOP LEVEL
of exactly one owning skill's ``scripts/`` directory. This module is the
spawn-side lookup: it never calls an LLM, never wraps the result in a tool
envelope, and fails open (``None``) on ambiguity, path escape, or a size cap.
"""

from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

from config import AUTO_SKILLS_DIR
from config.features import PTC

#: Plan keys are sha1 prefixes (hex); the fallback identity uses the same shape.
_PLAN_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: Keep the filename traversable and predictable. A step id that does not fit
#: is skipped instead of sanitized (two raw ids must never collapse to one file).
_STEP_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def is_safe_plan_key(plan_key: str) -> bool:
    return bool(plan_key) and bool(_PLAN_KEY_RE.fullmatch(plan_key))


def is_safe_step_id(step_id: str) -> bool:
    return bool(step_id) and bool(_STEP_ID_RE.fullmatch(step_id))


def script_filename(plan_key: str, step_id: str) -> str | None:
    """The exact basename for one step, or ``None`` when either id is unsafe."""
    if not is_safe_plan_key(plan_key) or not is_safe_step_id(step_id):
        return None
    return f"ptc__{plan_key}__{step_id}.py"


def find_step_script(
    plan_key: str,
    step_id: str,
    *,
    root: Path | None = None,
) -> Path | None:
    """Find the unique compiled script for ``(plan_key, step_id)``.

    Only ``<skill>/scripts/<exact name>`` candidates are considered. Zero
    candidates, multiple candidates, a symlink, a path outside the auto-skills
    root, or an unreadable/oversized file all return ``None`` (React fallback).
    """
    filename = script_filename(plan_key, step_id)
    if filename is None:
        return None
    base = Path(root or AUTO_SKILLS_DIR)
    try:
        base_resolved = base.resolve()
    except OSError:
        return None

    candidates: list[Path] = []
    try:
        for path in base.glob(f"**/scripts/{filename}"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                resolved = path.resolve()
                resolved.relative_to(base_resolved)
            except (OSError, ValueError):
                logger.warning("PTC replay: refusing a script outside the auto root: {}", path)
                continue
            candidates.append(resolved)
    except OSError:
        logger.exception("PTC replay: script scan failed under {}", base)
        return None

    if not candidates:
        return None
    if len(candidates) > 1:
        logger.warning(
            "PTC replay: ambiguous script for plan={} step={} ({}); falling back to React",
            plan_key,
            step_id,
            [str(path) for path in sorted(candidates)],
        )
        return None
    return candidates[0]


def read_step_script(
    plan_key: str,
    step_id: str,
    *,
    root: Path | None = None,
    max_bytes: int | None = None,
) -> str | None:
    """Read a compiled script, bounded by ``PTC['ptc_script_max_bytes']``."""
    path = find_step_script(plan_key, step_id, root=root)
    if path is None:
        return None
    limit = int(max_bytes if max_bytes is not None else PTC["ptc_script_max_bytes"])
    try:
        size = path.stat().st_size
        if size > limit:
            logger.warning(
                "PTC replay: script {} is {} bytes (limit {}); falling back to React",
                path,
                size,
                limit,
            )
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("PTC replay: cannot read {}: {}", path, exc)
        return None
