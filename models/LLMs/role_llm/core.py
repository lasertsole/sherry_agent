"""Per-role subagent model families (``EXECUTOR_LLM_*`` / …).

One family per functional role. A configured family (provider or api_base, or
``MODEL_LOCAL=true``) serves every child of that role; an unconfigured family
answers ``role_llm_configured`` False so the spawn resolver keeps the role's own
model tier (the shipped role definitions use ``AUXILIARY_LLM_*``). The builders
themselves fall back to the MAIN model when a family is empty — the documented
"per-role family → main" contract for a direct caller.

See :mod:`models.LLMs.family_llm` for the three-way rule and the fail-open
build.
"""

from __future__ import annotations

from typing import Any

from models.LLMs.family_llm import build_family_llm, family_configured

__all__ = [
    "ROLE_LLM_FAMILIES",
    "build_role_llm",
    "role_family",
    "role_llm_configured",
]

#: Functional role name → its model-family prefix. ``general`` has no family:
#: it is the identity role and keeps the depth/tier behavior.
ROLE_LLM_FAMILIES: dict[str, str] = {
    "executor": "EXECUTOR_LLM",
    "researcher": "RESEARCHER_LLM",
    "reviewer": "REVIEWER_LLM",
    "librarian": "LIBRARIAN_LLM",
}


def role_family(role: str) -> str | None:
    """The family prefix serving *role*, or ``None`` for an unmapped role."""
    return ROLE_LLM_FAMILIES.get(str(role).strip().lower())


def role_llm_configured(role: str) -> bool:
    """Whether *role*'s family is deliberately configured (remote or local)."""
    family = role_family(role)
    return bool(family and family_configured(family))


def build_role_llm(role: str, temperature: float | None = None) -> Any:
    """Create a fresh model for *role*'s family, falling back to MAIN.

    Raises:
        ValueError: *role* has no family (``general`` or an unknown name).
    """
    family = role_family(role)
    if family is None:
        raise ValueError(f"no per-role model family for role {role!r}")
    return build_family_llm(family, temperature, fallback="main")
