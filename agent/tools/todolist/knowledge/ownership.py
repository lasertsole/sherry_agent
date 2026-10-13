"""Plan-association sources for the ``knowledge`` tool (raw-reference layer).

The knowledge store is isolated per **plan identity** (``identity.py``), derived
from the canonical plan path rather than the bare plan name. This module owns
the association inputs that identity resolution builds on: a session is
associated with a plan when any of three sources links them:

1. the session's ``plan_ref`` state key (``state_register_db``);
2. a ``plan_ref`` on one of this session's todos (``todos.db``, filtered by
   ``session_id`` in SQL);
3. a boulder work (``config.path.resolve_boulder_path``) whose ``plan_name``
   matches and whose ``session_ids`` list contains this session
   (multi-session collaboration).

:func:`association_plan_refs` returns those links as :class:`PlanRef` records.
Each carries ``anchor_session_id`` — the session whose plans directory anchors
a *bare* reference: the owning session for state/todos refs, the first listed
session for a boulder work, so every collaborator of one work resolves to the
same physical plan file.

The name-level helpers (``is_plan_associated`` / ``associated_plan_names``)
delegate to identity resolution, so there is a single association contract.

Every read is fail-open: a missing/corrupt boulder file, a store error or an
empty ``session_id`` yields no references (deny), never an exception.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from loguru import logger

from agent.tools.todolist.registry.store_sqlite import get_todos_sync
from config.path import resolve_boulder_path
from runtime import StateKey, state_register_db

_PLAN_REF_STATE_KEY = StateKey.PLAN_REF

PlanRefSource = Literal["state", "todos", "boulder"]


@dataclass(frozen=True, slots=True)
class PlanRef:
    """One raw plan reference from an association source.

    ``anchor_session_id`` anchors a bare reference (``x`` / ``x.md``) to the
    ``SESSIONS_DIR/<anchor>/plans/`` directory. State/todos refs anchor to the
    owning session; boulder refs anchor to the work's first listed session so
    all collaborators resolve the same path.
    """

    ref: str
    source: PlanRefSource
    anchor_session_id: str


def _plan_name_of(ref: object) -> str:
    """Map a plan reference (path or bare name) to its plan-name stem."""
    if isinstance(ref, str) and ref.strip():
        return Path(ref.strip()).stem
    return ""


def _state_plan_refs(session_id: str) -> list[PlanRef]:
    """Plan refs from this session's ``plan_ref`` state key."""
    try:
        value = state_register_db.get_state(session_id, _PLAN_REF_STATE_KEY, "")
    except Exception:
        logger.debug(
            "knowledge ownership: state plan_ref read failed for {}", session_id, exc_info=True
        )
        return []
    if isinstance(value, str) and value.strip():
        return [PlanRef(ref=value.strip(), source="state", anchor_session_id=session_id)]
    return []


def _todo_plan_refs(session_id: str) -> list[PlanRef]:
    """Plan refs from this session's todos (store filters by session in SQL)."""
    try:
        todos = get_todos_sync(session_id)
    except Exception:
        logger.debug("knowledge ownership: todos read failed for {}", session_id, exc_info=True)
        return []
    refs: list[PlanRef] = []
    for todo in todos:
        ref = todo.get("plan_ref")
        if isinstance(ref, str) and ref.strip():
            refs.append(PlanRef(ref=ref.strip(), source="todos", anchor_session_id=session_id))
    return refs


def iter_boulder_works(boulder: object) -> Iterator[object]:
    """Yield works from a boulder document (dict-keyed or list-shaped)."""
    if not isinstance(boulder, dict):
        return
    works = boulder.get("works")
    if isinstance(works, dict):
        yield from works.values()
    elif isinstance(works, list):
        yield from works


def read_boulder_document() -> object | None:
    """Read and parse the boulder document; None when absent or corrupt."""
    boulder_path = resolve_boulder_path()
    try:
        if not boulder_path.is_file():
            return None
        return json.loads(boulder_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.debug("knowledge ownership: boulder read failed at {}", boulder_path, exc_info=True)
        return None


def _boulder_plan_refs(session_id: str, plan_name: str | None) -> list[PlanRef]:
    """Plan refs of boulder works whose ``session_ids`` include this session.

    With *plan_name*, only works whose ``plan_name`` matches are returned (the
    historical boulder association rule). The ref is the work's canonical
    ``active_plan`` path; when it is absent (malformed work), the bare
    ``plan_name`` is anchored to the work's first listed session so all
    collaborators still resolve one shared path.
    """
    boulder = read_boulder_document()
    if boulder is None:
        return []

    refs: list[PlanRef] = []
    for work in iter_boulder_works(boulder):
        if not isinstance(work, dict):
            continue
        work_name = work.get("plan_name")
        session_ids = work.get("session_ids")
        if not isinstance(work_name, str) or not work_name.strip():
            continue
        if plan_name is not None and work_name.strip() != plan_name:
            continue
        if not isinstance(session_ids, list) or session_id not in session_ids:
            continue
        listed = [item for item in session_ids if isinstance(item, str) and item.strip()]
        anchor = listed[0].strip() if listed else session_id
        active_plan = work.get("active_plan")
        ref = (
            active_plan.strip()
            if isinstance(active_plan, str) and active_plan.strip()
            else work_name.strip()
        )
        refs.append(PlanRef(ref=ref, source="boulder", anchor_session_id=anchor))
    return refs


def association_plan_refs(session_id: str, plan_name: str | None = None) -> list[PlanRef]:
    """Every raw plan reference this session is associated with.

    With *plan_name*, state/todos refs match on their plan-name stem and
    boulder refs match on the work's ``plan_name`` field (the historical
    association semantics). Without it, every associated ref is returned.
    """
    sid = session_id.strip() if isinstance(session_id, str) else ""
    if not sid:
        logger.debug("knowledge ownership: empty session_id; no plan association")
        return []
    wanted = plan_name.strip() if isinstance(plan_name, str) and plan_name.strip() else None
    refs = [*_state_plan_refs(sid), *_todo_plan_refs(sid), *_boulder_plan_refs(sid, wanted)]
    if wanted is None:
        return refs
    return [ref for ref in refs if ref.source == "boulder" or _plan_name_of(ref.ref) == wanted]


def associated_plan_names(session_id: str) -> set[str]:
    """Every plan name this session may access (name-level view of identity)."""
    from .identity import associated_plan_identities

    return {identity.plan_name for identity in associated_plan_identities(session_id)}


def is_plan_associated(session_id: str, plan_name: str) -> bool:
    """True when this session may access knowledge for *plan_name*."""
    if not isinstance(plan_name, str) or not plan_name.strip():
        return False
    from .identity import resolve_plan_identity

    return resolve_plan_identity(session_id, plan_name) is not None


__all__ = [
    "PlanRef",
    "PlanRefSource",
    "association_plan_refs",
    "associated_plan_names",
    "is_plan_associated",
    "iter_boulder_works",
    "read_boulder_document",
]
