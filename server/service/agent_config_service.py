"""Per-session agent configuration — 预设 的四栏（工具 / 中间件 / 子代理模型 / 技能）。

One JSON payload under ``StateKey.AGENT_CONFIG`` (parked twin
``AGENT_CONFIG_PENDING`` for a mid-turn write, promoted by the same
``turn_runner.on_turn_finished`` hook as the model/thinking controls)::

    {
      "tools": ["read_file", ...] | null,        # null = every tool enabled
      "middlewares_disabled": ["TaskIntentMiddleware"],   # [] = every switch on
      "subagent_models": {"researcher": {<profile>} | null},
      "skills": ["skill-a", ...] | null          # null = every skill in the index
    }

Validation is the whole point of this module: a payload naming an unknown tool,
skill or role, or a REQUIRED middleware, is rejected loudly (400), so a typo can
never silently widen what a session may do. ``subagent_models`` descriptors go
through the same sanitizer the main-model override uses (``model`` required,
optional fields whitelisted), which means a role can point at another provider /
key, not merely at another model name.

``skills`` is the one field that changes the SYSTEM PROMPT (the
``<available_skills>`` index), which ``system_prompt_injection`` caches per
session in mem + db: :func:`invalidate_session_prompt` therefore clears that
cache whenever a payload lands — at the live write AND when a parked choice is
promoted at the turn boundary (``session_settings_service`` calls it from the
promotion loop). Without it a skill change would only take effect after the next
context compression.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from runtime import StateKey, state_register_db, state_register_mem

__all__ = [
    "AgentConfigError",
    "apply_agent_config_choice",
    "get_agent_config_state",
    "invalidate_session_prompt",
    "sanitize_agent_config",
]

#: Top-level payload keys (unknown keys are rejected).
_CONFIG_FIELDS = ("tools", "middlewares_disabled", "subagent_models", "skills")


class AgentConfigError(ValueError):
    """A rejected payload — the HTTP layer turns it into a 400."""


def _tool_names() -> set[str]:
    """Every main-agent tool name (the catalogue's own list)."""
    from agent.tools.catalog import tool_catalog

    names = {entry["name"] for entry in tool_catalog()}
    if names:
        return names
    # A stubbed ``agent.tools`` (a test process that installs sys.modules stubs)
    # would whitelist NOTHING and reject every payload; fall back to the static
    # group map, which is the catalogue's own name source.
    from agent.tools.catalog import TOOL_GROUPS

    return {name for names_in_group in TOOL_GROUPS.values() for name in names_in_group}


def _required_tool_names() -> set[str]:
    """Tools every payload must keep (the catalogue's own required set)."""
    from agent.tools.catalog import REQUIRED_TOOLS

    return set(REQUIRED_TOOLS)


def _skill_names() -> set[str]:
    """Every skill the index can contain (the catalogue's own list)."""
    from skills.loader import skills_catalog

    return {entry["name"] for entry in skills_catalog()}


def invalidate_session_prompt(session_id: str) -> None:
    """Drop the cached system prompt of *session_id* (mem + db tiers).

    Called after an agent-config payload lands: ``skills`` feeds the
    ``<available_skills>`` index inside that prompt, and the three-tier cache in
    ``agent.middlewares.system_prompt`` would otherwise keep serving the old
    index until a compression happens to rewrite it. Deleting only the
    ``SYSTEM_PROMPT`` key leaves the frozen persona snapshot (``workspace``)
    alone — the persona stays fixed per session by design.

    Best-effort: a register failure must never fail the config write itself.
    """
    for register in (state_register_mem, state_register_db):
        try:
            register.delete_state(session_id, StateKey.SYSTEM_PROMPT)
        except Exception:  # noqa: BLE001 - a cache clear cannot break the write
            logger.exception("agent_config: failed to clear the cached system prompt")


def _gateable_middleware_names() -> set[str]:
    from agent.middlewares.catalog import GATEABLE_MIDDLEWARES

    return set(GATEABLE_MIDDLEWARES)


def _middleware_names() -> set[str]:
    from agent.middlewares.catalog import MIDDLEWARE_ORDER

    return set(MIDDLEWARE_ORDER)


def _role_names() -> set[str]:
    from agent.tools.subagent.types.functional_role import FunctionalRole

    return {role.value for role in FunctionalRole}


def _clean_profile(role: str, profile: object) -> dict[str, str] | None:
    """Sanitize one role's model profile (``None`` = follow the role tier)."""
    if profile is None:
        return None
    from server.service.session_settings_service import sanitize_main_model_override

    try:
        return sanitize_main_model_override(profile)
    except ValueError as exc:
        raise AgentConfigError(f"subagent_models.{role}: {exc}") from exc


def sanitize_agent_config(payload: object) -> dict[str, Any]:
    """Validate + normalize one payload (raises :class:`AgentConfigError`).

    Only the fields present are kept: an absent field means "no opinion" (every
    tool on / every switch on / role tiers), so a partial payload can be sent.
    """
    if not isinstance(payload, dict):
        raise AgentConfigError("agent config must be an object")
    unknown = sorted(set(payload) - set(_CONFIG_FIELDS))
    if unknown:
        raise AgentConfigError(f"unknown field(s): {', '.join(unknown)}")

    cleaned: dict[str, Any] = {}

    if "tools" in payload:
        tools = payload["tools"]
        if tools is None:
            cleaned["tools"] = None
        else:
            if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
                raise AgentConfigError("'tools' must be a list of names or null")
            known = _tool_names()
            wanted = [t.strip() for t in tools if t.strip()]
            missing = sorted(set(wanted) - known)
            if missing:
                raise AgentConfigError(f"unknown tool(s): {', '.join(missing)}")
            # REQUIRED tools can never be left out (the 工具 tab shows them locked).
            dropped_required = sorted(_required_tool_names() - set(wanted))
            if dropped_required:
                raise AgentConfigError(
                    f"required tool(s) cannot be disabled: {', '.join(dropped_required)}"
                )
            # Dedup, keep the caller's order (the UI sends catalogue order).
            cleaned["tools"] = list(dict.fromkeys(wanted))

    if "middlewares_disabled" in payload:
        disabled = payload["middlewares_disabled"]
        if not isinstance(disabled, list) or any(not isinstance(m, str) for m in disabled):
            raise AgentConfigError("'middlewares_disabled' must be a list of names")
        known = _middleware_names()
        gateable = _gateable_middleware_names()
        wanted = [m.strip() for m in disabled if m.strip()]
        unknown_mw = sorted(set(wanted) - known)
        if unknown_mw:
            raise AgentConfigError(f"unknown middleware(s): {', '.join(unknown_mw)}")
        locked = sorted(set(wanted) - gateable)
        if locked:
            raise AgentConfigError(
                f"middleware(s) cannot be disabled (system-required): {', '.join(locked)}"
            )
        cleaned["middlewares_disabled"] = list(dict.fromkeys(wanted))

    if "skills" in payload:
        skills = payload["skills"]
        if skills is None:
            cleaned["skills"] = None
        else:
            if not isinstance(skills, list) or any(not isinstance(s, str) for s in skills):
                raise AgentConfigError("'skills' must be a list of names or null")
            known_skills = _skill_names()
            wanted_skills = [s.strip() for s in skills if s.strip()]
            unknown_skills = sorted(set(wanted_skills) - known_skills)
            if unknown_skills:
                raise AgentConfigError(f"unknown skill(s): {', '.join(unknown_skills)}")
            # An EMPTY list is a legal, explicit choice: no skill in the index.
            cleaned["skills"] = list(dict.fromkeys(wanted_skills))

    if "subagent_models" in payload:
        models = payload["subagent_models"]
        if not isinstance(models, dict):
            raise AgentConfigError("'subagent_models' must be an object")
        known_roles = _role_names()
        unknown_roles = sorted(set(models) - known_roles)
        if unknown_roles:
            raise AgentConfigError(f"unknown subagent role(s): {', '.join(unknown_roles)}")
        cleaned["subagent_models"] = {
            role: _clean_profile(role, profile) for role, profile in models.items()
        }

    return cleaned


def get_agent_config_state(session_id: str) -> dict[str, Any]:
    """The session's effective config: ``{"config": {...}, "pending": bool}``.

    A parked (mid-turn) choice wins over the live key — the caller is asking
    what the NEXT turn will run with. Reads rehydrate mem from the durable
    mirror, so a restart cannot silently drop the config.
    """
    from server.service.session_settings_service import _pending_value, _read_value

    if not session_id:
        return {"config": {}, "pending": False}
    parked, parked_value = _pending_value(session_id, StateKey.AGENT_CONFIG_PENDING)
    value = parked_value if parked else _read_value(session_id, StateKey.AGENT_CONFIG)
    return {"config": value if isinstance(value, dict) else {}, "pending": parked}


async def apply_agent_config_choice(session_id: str, payload: object) -> tuple[dict, bool]:
    """HTTP entry for ``PUT /sessions/agent_config``; returns ``(config, pending)``.

    Validated up front (a rejected payload never touches the registers), then
    applied under the shared session lock: while a turn is in flight the choice
    is PARKED so the running turn keeps the tool set it started with, and the
    turn boundary promotes it (the same contract as the model/thinking twins).
    """
    from server.service.session_settings_service import (
        _apply_session_choice,
        _delete_value,
        _write_value,
    )

    cleaned = sanitize_agent_config(payload)
    if not session_id:
        raise ValueError("invalid session_id")

    def _write_live() -> None:
        """Straight to the live key: a live write supersedes a parked choice."""
        _write_value(session_id, StateKey.AGENT_CONFIG, cleaned)
        _delete_value(session_id, StateKey.AGENT_CONFIG_PENDING)

    pending = await _apply_session_choice(
        session_id,
        live_key=StateKey.AGENT_CONFIG,
        pending_key=StateKey.AGENT_CONFIG_PENDING,
        value=cleaned,
        write_live=_write_live,
    )
    # The skills selection is part of the system prompt: a LIVE write must drop
    # the cached prompt now. A parked one is invalidated at promotion instead
    # (the live key still holds the old value until then, so clearing early
    # would only rebuild the same prompt).
    if not pending:
        invalidate_session_prompt(session_id)
    logger.info(
        "Agent config {} for session {}: tools={} middlewares_disabled={} "
        "subagent_models={} skills={}",
        "parked" if pending else "applied",
        session_id,
        None if cleaned.get("tools") is None else len(cleaned["tools"]),
        len(cleaned.get("middlewares_disabled") or []),
        len(cleaned.get("subagent_models") or {}),
        None if cleaned.get("skills") is None else len(cleaned["skills"]),
    )
    return cleaned, pending
