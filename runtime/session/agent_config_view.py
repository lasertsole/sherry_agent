"""Read-only views over the session agent config (``StateKey.AGENT_CONFIG``).

The payload itself is written by ``PUT /sessions/agent_config`` (服务端
``server/service/agent_config_service.py``) and read on hot paths by
:mod:`workspace.prompt_builder` (the ``<available_skills>`` index). That caller
lives in ``workspace/**``, which import-linter keeps away from ``agent/**`` — so
this module is the shared leaf, depending only on ``runtime`` (mirroring how
:mod:`runtime.session.project_dir` serves the same packages).

Mem-register only and fail-open: it runs inside a prompt build (no blocking
I/O), and a broken register must never change behaviour — every failure
degrades to ``None``, which reads as "no opinion" at every call site.
"""

from __future__ import annotations

from typing import Any

__all__ = ["session_skill_names"]


def session_skill_names(session_id: str | None) -> list[str] | None:
    """The session's explicitly selected skill names, or ``None`` for "no opinion".

    ``None`` (unset session, no ``skills`` key, malformed payload, broken
    register) means every skill keeps entering the prompt index — the
    behaviour of every session that never touched 预设-技能. An EXPLICIT list —
    including an empty one, which selects no skill at all — is authoritative.
    """
    if not session_id:
        return None
    try:
        from runtime import StateKey, state_register_mem

        raw: Any = state_register_mem.get_state(session_id, StateKey.AGENT_CONFIG, None)
        if not isinstance(raw, dict):
            return None
        names = raw.get("skills")
        if not isinstance(names, list):
            return None
        return [str(name) for name in names if isinstance(name, str) and name.strip()]
    except Exception:  # noqa: BLE001 - never break a prompt build over the config read
        return None
