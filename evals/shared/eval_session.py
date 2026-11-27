"""Session-binding helpers shared by every eval suite entry point.

The two binding paths (``coding_capability._bind_session`` and
``external_bench.bind_external_session``) both grant HITL approvals through
this helper. Without it a high-risk tool call parks the turn behind a human
approval no eval harness can answer, and the task hangs until timeout.

The agent-runtime import stays lazy so this module remains importable without
the server -- the shared-package contract.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def set_session_yolo(session_id: str) -> None:
    """Auto-grant approvals for an eval session (idempotent)."""
    if not session_id:
        return
    from agent.middlewares.humanInTheLoop.approval import set_session_yolo as _grant

    _grant(str(session_id))


def set_session_experience_extraction(session_id: str, *, enabled: bool = True) -> None:
    """Override one eval session's experience-learning switch (fail-open).

    The global ``sherry.jsonc`` value is left untouched: this writes only the
    session's ``skill_extraction_enabled`` override, so every inherited field
    (flow compilation, iteration ceilings) keeps its global value. The parked
    twin is removed so a write made while a turn is running cannot be held back
    until a later turn boundary.
    """
    if not session_id:
        return
    from runtime.session.state_keys import StateKey
    from server.service.session_settings_service import _delete_value, _write_value

    _write_value(str(session_id), StateKey.EXTRACTION, {"skill_extraction_enabled": bool(enabled)})
    _delete_value(str(session_id), StateKey.EXTRACTION_PENDING)


def experience_extraction_evidence(session_id: str) -> dict[str, object]:
    """Read the extraction trace a task left behind (never raises).

    ``fired`` means the plan-completion hook or an eval-forced pass scheduled
    the extraction. ``snapshot_hash`` is the plan fingerprint recorded when a
    pass CONCLUDED — which includes the "task judged non-generalizable" skip,
    where nothing is written; it is therefore not a production signal (the
    ``produced`` flag below is). Both are read mem-first, like the extraction
    gates.
    """
    if not session_id:
        return {"fired": False, "snapshot_hash": ""}
    try:
        from runtime import state_register_db, state_register_mem
        from runtime.session.state_keys import StateKey

        def _read(key: object, default: object) -> object:
            value = state_register_mem.get_state(session_id, key, None)  # type: ignore[arg-type]
            if value is None:
                value = state_register_db.get_state(session_id, key, None)  # type: ignore[arg-type]
            return default if value is None else value

        return {
            "fired": bool(_read(StateKey.SKILL_REVIEW_FIRED, False)),
            "snapshot_hash": str(_read(StateKey.SKILL_REVIEW_SNAPSHOT_HASH, "") or ""),
        }
    except Exception:  # noqa: BLE001 - an eval report must never crash on telemetry
        return {"fired": False, "snapshot_hash": ""}


async def force_experience_extraction(session_id: str) -> dict[str, object]:
    """Force the product skill-review pass for an eval session (eval-only).

    L1 DeepSWE runs need extraction evidence even when the product's
    plan-completion hook did not schedule the pass. This calls the product pass
    directly from the eval side without touching product code. The fired marker
    is written before the call so a forced attempt stays observable even when
    the pass no-ops or raises; failures are fail-open and never break the eval.

    ``produced`` is measured here, not inferred from ``snapshot_hash``: the
    runner also records that hash when it SKIPS the review as non-generalizable
    (nothing written), so the hash alone over-reports production. The auto-skill
    tree is snapshotted around the pass and any changed/created/deleted file is
    reported in ``produced_paths``.
    """
    if not session_id:
        return {"fired": False, "snapshot_hash": "", "produced": False}
    produced_paths: list[str] = []
    before: object = None
    try:
        from agent.middlewares.summarization.skill_review import snapshot_auto_skills

        before = snapshot_auto_skills()
    except Exception:  # noqa: BLE001 - telemetry only
        before = None
    try:
        from agent.middlewares.summarization.background_reviews import _SKILL_REVIEW_LOCK_KEY
        from agent.middlewares.summarization.skill_review_runner import _run_skill_review
        from runtime import state_register_db, state_register_mem
        from runtime.session.state_keys import StateKey

        state_register_db.set_state(session_id, StateKey.SKILL_REVIEW_FIRED, True)
        state_register_mem.set_state(session_id, _SKILL_REVIEW_LOCK_KEY, False)
        await _run_skill_review(session_id)
    except Exception:  # noqa: BLE001 - an eval-forced pass must never fail the run
        logger.exception("forced experience extraction failed for %s", session_id)
    if before is not None:
        try:
            from agent.middlewares.summarization.skill_review import (
                changed_skill_paths,
                snapshot_auto_skills,
            )

            produced_paths = sorted(changed_skill_paths(before, snapshot_auto_skills()))
        except Exception:  # noqa: BLE001 - telemetry only
            produced_paths = []
    return {
        **experience_extraction_evidence(session_id),
        "produced": bool(produced_paths),
        "produced_paths": produced_paths[:20],
    }
