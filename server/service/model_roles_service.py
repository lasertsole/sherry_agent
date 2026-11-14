"""Effective model per judge / subagent role (the Model Config inheritance labels).

The 模型配置 panel edits model families, but a family's EMPTY form does not mean
"unconfigured": the backend quietly inherits another model. This service answers
the question the form cannot — *which model will this role actually run on, and
why* — without building a client or spending a token.

Sources:

* ``configured`` — the family's own ``PROVIDER`` / ``API_BASE`` is set;
* ``local`` — ``<PREFIX>_MODEL_LOCAL=true`` reuses the fallback family's GGUF;
* ``auxiliary`` — TaskFlow / the completion judge follow ``AUXILIARY_LLM_*``;
* ``main`` — a role whose definition declares ``model_tier: main``;
* ``depth`` — an ``inherit`` role, decided by the child's depth role.

Everything is fail-open: a missing ``.models`` or role definition answers the
shipped defaults instead of failing the panel.
"""

from __future__ import annotations

from typing import Any

from config.models_settings import MODEL_SETTING_DEFAULTS
from server.service.models_config import read_models_raw_values

__all__ = ["read_model_roles"]

#: (role id, family prefix, fallback source). ``completion_judge`` reuses the
#: auxiliary family by design (there is no CJ_LLM_* family).
_ROLE_ROWS: tuple[tuple[str, str, str], ...] = (
    ("step_judge", "TASKFLOW_LLM", "auxiliary"),
    ("completion_judge", "AUXILIARY_LLM", "auxiliary"),
    ("executor", "EXECUTOR_LLM", "role"),
    ("researcher", "RESEARCHER_LLM", "role"),
    ("reviewer", "REVIEWER_LLM", "role"),
    ("librarian", "LIBRARIAN_LLM", "role"),
)


def _values() -> dict[str, str]:
    """The live ``.models`` values over the shipped defaults (scalars only)."""
    values = {
        key: str(default)
        for key, default in MODEL_SETTING_DEFAULTS.items()
        if isinstance(default, (str, int, float, bool))
    }
    values.update(read_models_raw_values())
    return values


def _role_tier(role: str) -> str:
    """The role definition's ``model_tier`` ("main" / "auxiliary" / "inherit")."""
    try:
        from agent.tools.subagent.roles.loader import load_role_definition
        from agent.tools.subagent.types.functional_role import FunctionalRole

        definition = load_role_definition(FunctionalRole(role))
        tier = str(getattr(definition, "model_tier", "") or "")
        return tier if tier in ("main", "auxiliary") else "inherit"
    except Exception:  # noqa: BLE001 - an unreadable definition keeps the shipped tier
        return "auxiliary"


def _max_output(values: dict[str, str], family: str) -> int | None:
    """The family's output cap as stored (None = empty / unparsable)."""
    raw = values.get(f"{family}_MAX_OUTPUT_TOKEN", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _row(values: dict[str, str], role: str, family: str, fallback: str) -> dict[str, Any]:
    """One role's effective model + why."""
    provider = values.get(f"{family}_PROVIDER", "").strip()
    api_base = values.get(f"{family}_API_BASE", "").strip()
    local = values.get(f"{family}_MODEL_LOCAL", "").strip().lower() == "true"
    configured = bool(provider or api_base)

    if role == "completion_judge":
        # The completion judge IS the auxiliary family; no independent family.
        source = "auxiliary"
        effective_provider = values.get("AUXILIARY_LLM_PROVIDER", "").strip()
        effective_model = values.get("AUXILIARY_LLM_API_NAME", "").strip()
    elif local:
        source = "local"
        if fallback == "role":
            effective_provider = values.get("MAIN_LLM_PROVIDER", "").strip()
            effective_model = values.get("MAIN_LLM_NAME", "").strip()
        else:
            effective_provider = values.get("AUXILIARY_LLM_PROVIDER", "").strip()
            effective_model = values.get("AUXILIARY_LLM_API_NAME", "").strip()
    elif configured:
        source = "configured"
        effective_provider = provider
        effective_model = values.get(f"{family}_API_NAME", "").strip()
    else:
        tier = fallback if fallback != "role" else _role_tier(role)
        if tier == "main":
            source = "main"
            effective_provider = values.get("MAIN_LLM_PROVIDER", "").strip()
            effective_model = values.get("MAIN_LLM_NAME", "").strip()
        elif tier == "auxiliary":
            source = "auxiliary"
            effective_provider = values.get("AUXILIARY_LLM_PROVIDER", "").strip()
            effective_model = values.get("AUXILIARY_LLM_API_NAME", "").strip()
        else:
            # Depth role decides (ORCHESTRATOR → main, LEAF → auxiliary); the
            # panel cannot know the depth before a spawn, so it says so.
            source = "depth"
            effective_provider = ""
            effective_model = ""

    return {
        "role": role,
        "family": family,
        "configured": configured,
        "local": local,
        "source": source,
        "effective_provider": effective_provider,
        "effective_model": effective_model,
        "max_output_tokens": _max_output(values, family),
    }


def read_model_roles() -> dict[str, list[dict[str, Any]]]:
    """Every judge / role row: the effective model and its inheritance source."""
    values = _values()
    return {"roles": [_row(values, *entry) for entry in _ROLE_ROWS]}
