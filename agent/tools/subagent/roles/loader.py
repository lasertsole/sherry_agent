"""Load functional role definitions from AGENTS.md files.

Built-in defaults ship INSIDE the package (tracked, distributable):
    agent/tools/subagent/roles/definitions/<name>/AGENTS.md

An optional per-user override may be placed (untracked) at:
    workspace/subagent_roles/<name>/AGENTS.md

Resolution order: workspace override → package default → None (caller falls
back to GENERAL defaults).

Parses YAML frontmatter (name, description, model_tier, tools) and the
markdown body as the role-specific system prompt supplement.
Mirrors deepagents' _load_local_subagents() pattern.
"""

from pathlib import Path
from dataclasses import dataclass

import yaml
from loguru import logger

from config import WORKSPACE_DIR
from ..types.functional_role import FunctionalRole

_OVERRIDE_DIR_NAME = "subagent_roles"
_DEFINITIONS_DIR = Path(__file__).resolve().parent / "definitions"
_VALID_MODEL_TIERS = frozenset({"main", "auxiliary", "inherit"})


@dataclass(frozen=True)
class RoleDefinition:
    """Parsed role definition from an AGENTS.md file."""

    role: FunctionalRole
    description: str
    model_tier: str  # "main" | "auxiliary" | "inherit"
    tools: list[str] | None  # None = inherit all; empty list = no tools
    prompt_body: str  # markdown body after frontmatter


_cache: dict[FunctionalRole, RoleDefinition] | None = None


def get_roles_dir() -> Path:
    """Return the tracked package directory holding the built-in definitions."""
    return _DEFINITIONS_DIR


def _override_dir_name() -> str:
    """Return the configured override directory name, falling back to the default."""
    try:
        from ..config import get_config

        return get_config().roles_override_dir_name or _OVERRIDE_DIR_NAME
    except Exception:
        return _OVERRIDE_DIR_NAME


def _role_file_candidates(role: FunctionalRole) -> tuple[Path, ...]:
    """Override first (untracked, user-editable), then the packaged default."""
    override = Path(WORKSPACE_DIR) / _override_dir_name() / role.value / "AGENTS.md"
    default = _DEFINITIONS_DIR / role.value / "AGENTS.md"
    return (override, default)


def _parse_role_file(role: FunctionalRole, text: str) -> RoleDefinition:
    """Parse an AGENTS.md document into a RoleDefinition.

    Raises ``ValueError`` for malformed frontmatter; callers fail open to None.
    """
    if not text.startswith("---"):
        raise ValueError("missing YAML frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("unterminated YAML frontmatter")
    frontmatter = yaml.safe_load(parts[1]) or {}
    if not isinstance(frontmatter, dict):
        raise ValueError("frontmatter is not a mapping")

    raw_tools = frontmatter.get("tools")
    tools: list[str] | None
    if raw_tools is None or raw_tools == "inherit":
        tools = None
    elif isinstance(raw_tools, list):
        tools = [str(name) for name in raw_tools]
    else:
        raise ValueError(f"invalid 'tools' value: {raw_tools!r}")

    model_tier = str(frontmatter.get("model_tier") or "inherit")
    if model_tier not in _VALID_MODEL_TIERS:
        model_tier = "inherit"

    return RoleDefinition(
        role=role,
        description=str(frontmatter.get("description") or ""),
        model_tier=model_tier,
        tools=tools,
        prompt_body=parts[2].strip(),
    )


def load_role_definition(role: FunctionalRole) -> RoleDefinition | None:
    """Load a single role definition, preferring a workspace override over the default.

    Returns ``None`` when no definition exists or the definition cannot be
    parsed — callers fall back to GENERAL defaults (fail-open).
    """
    for path in _role_file_candidates(role):
        if not path.is_file():
            continue
        try:
            return _parse_role_file(role, path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError) as e:
            logger.warning("Invalid role definition at {}: {}", path, e)
            return None
    return None


def load_all_role_definitions() -> dict[FunctionalRole, RoleDefinition]:
    """Load every known role's definition (override → default; missing → skipped).

    Results are cached; call invalidate_role_cache() to force reload.
    """
    global _cache
    if _cache is not None:
        return _cache
    result: dict[FunctionalRole, RoleDefinition] = {}
    for role in FunctionalRole:
        definition = load_role_definition(role)
        if definition is not None:
            result[role] = definition
    _cache = result
    return result


def invalidate_role_cache() -> None:
    """Clear the role definition cache (e.g. after workspace file edit)."""
    global _cache
    _cache = None
