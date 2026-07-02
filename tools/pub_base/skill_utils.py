"""Lightweight skill metadata utilities shared by prompt_builder and skills_tool.

This module intentionally avoids importing the tool registry, CLI config, or any
heavy dependency chain.  It is safe to import at module level without triggering
tool registration or provider resolution.
"""

import os
import re
import sys
from typing import Any
from pathlib import Path
from loguru import logger
from skills.loader import parse_frontmatter
from config import ROOT_DIR, AUTO_SKILLS_DIR

# ── Platform mapping ──────────────────────────────────────────────────────

PLATFORM_MAP = {
    "macos": "darwin",
    "linux": "linux",
    "windows": "win32",
}

EXCLUDED_SKILL_DIRS = frozenset((".git", ".github", ".hub", ".archive"))

# ── Lazy YAML loader ─────────────────────────────────────────────────────

_yaml_load_fn = None


def yaml_load(content: str):
    """Parse YAML with lazy import and CSafeLoader preference."""
    global _yaml_load_fn
    if _yaml_load_fn is None:
        import yaml

        loader = getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader

        def _load(value: str):
            return yaml.load(value, Loader=loader)

        _yaml_load_fn = _load
    return _yaml_load_fn(content)


# ── Frontmatter parsing ──────────────────────────────────────────────────


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from a markdown string.

    Uses yaml with CSafeLoader for full YAML support (nested metadata, lists)
    with a fallback to simple key:value splitting for robustness.

    Returns:
        (frontmatter_dict, remaining_body)
    """
    frontmatter: dict[str, Any] = {}
    body = content

    if not content.startswith("---"):
        return frontmatter, body

    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return frontmatter, body

    yaml_content = content[3 : end_match.start() + 3]
    body = content[end_match.end() + 3 :]

    try:
        parsed = yaml_load(yaml_content)
        if isinstance(parsed, dict):
            frontmatter = parsed
    except Exception:
        # Fallback: simple key:value parsing for malformed YAML
        for line in yaml_content.strip().split("\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            frontmatter[key.strip()] = value.strip()

    return frontmatter, body


# ── Platform matching ─────────────────────────────────────────────────────


def skill_matches_platform(frontmatter: dict[str, Any]) -> bool:
    """Return True when the skill is compatible with the current OS.

    Skills declare platform requirements via a top-level ``platforms`` list
    in their YAML frontmatter::

        platforms: [macos]          # macOS only
        platforms: [macos, linux]   # macOS and Linux

    If the field is absent or empty the skill is compatible with **all**
    platforms (backward-compatible default).
    """
    platforms = frontmatter.get("platforms")
    if not platforms:
        return True
    if not isinstance(platforms, list):
        platforms = [platforms]
    current = sys.platform
    for platform in platforms:
        normalized = str(platform).lower().strip()
        mapped = PLATFORM_MAP.get(normalized, normalized)
        if current.startswith(mapped):
            return True
    return False

# Storage prefix: all skill config vars are stored under skills.config.*
# in config.yaml.  Skill authors declare logical keys (e.g. "wiki.path");
# the system adds this prefix for storage and strips it for display.
SKILL_CONFIG_PREFIX = "skills.config"

# ── Description extraction ────────────────────────────────────────────────


def extract_skill_description(frontmatter: dict[str, Any]) -> str:
    """Extract a truncated description from parsed frontmatter."""
    raw_desc = frontmatter.get("description", "")
    if not raw_desc:
        return ""
    desc = str(raw_desc).strip().strip("'\"")
    if len(desc) > 60:
        return desc[:57] + "..."
    return desc


# ── File iteration ────────────────────────────────────────────────────────


def iter_skill_index_files(skills_dir: Path, filename: str):
    """Walk skills_dir yielding sorted paths matching *filename*.

    Excludes ``.git``, ``.github``, ``.hub``, ``.archive`` directories.
    """
    matches = []
    for root, dirs, files in os.walk(skills_dir, followlinks=True):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_SKILL_DIRS]
        if filename in files:
            matches.append(Path(root) / filename)
    for path in sorted(matches, key=lambda p: str(p.relative_to(skills_dir))):
        yield path


# ── Namespace helpers for plugin-provided skills ───────────────────────────

_NAMESPACE_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def parse_qualified_name(name: str) -> tuple[str | None, str]:
    """Split ``'namespace:skill-name'`` into ``(namespace, bare_name)``.

    Returns ``(None, name)`` when there is no ``':'``.
    """
    if ":" not in name:
        return None, name
    return tuple(name.split(":", 1))  # type: ignore[return-value]


def is_valid_namespace(candidate: str | None) -> bool:
    """Check whether *candidate* is a valid namespace (``[a-zA-Z0-9_-]+``)."""
    if not candidate:
        return False
    return bool(_NAMESPACE_RE.match(candidate))

def find_auto_skills(*, skip_disabled: bool = False) -> list[dict[str, Any]]:
    skills: list[dict[str, Any]] = []
    seen_paths = set()  # 用于去重

    for skill_file in AUTO_SKILLS_DIR.glob("**/SKILL.md"):
        if skill_file in seen_paths:
            continue
        seen_paths.add(skill_file)

        content = skill_file.read_text(encoding="utf-8")
        meta, _ = parse_frontmatter(content)
        name = str(meta.get("name", skill_file.parent.name))
        desc = str(meta.get("description", ""))
        rel = skill_file.relative_to(ROOT_DIR)
        skills.append(
            {
                "name": name,
                "description": desc,
                "location": f"./{rel.as_posix()}",
            }
        )

    skills.sort(key=lambda x: x["name"])
    return skills

def sort_skills(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep every skill listing path ordered the same way."""
    return sorted(skills, key=lambda s: (s.get("category") or "", s["name"]))

def get_all_auto_skills_dirs()-> list[Path]:
    skills_dirs: list[Path] = []
    for skill_file in AUTO_SKILLS_DIR.glob("**/SKILL.md"):
        skills_dirs.append(skill_file.parent)
    return skills_dirs