"""Plan identity: the physical storage key behind a plan name.

The knowledge store used to be keyed by the bare plan name, so two sessions
that each owned a ``plans/x.md`` wrote into one shared ``x/`` directory and
silently overwrote each other. Identity fixes that: the storage key derives
from the **canonical plan path**, resolved through the session's association
sources (``ownership.association_plan_refs``) — never from the name:

- same literal name, different plan files (per-session plans) -> different
  keys -> physically isolated;
- one plan file shared through a boulder work's ``session_ids`` -> every
  listed session resolves the same path -> same key -> collaboration intact.

A session with no resolvable plan file still owns a session-derived fallback
identity named ``session-<sha1(session_id)[:8]>`` (the nudge's fallback plan
name). The full-id hash replaces the old ``session_id[:8]`` slice, so two ids
sharing an 8-char prefix can no longer collide.

``key = sha1(repo-relative normalized path)[:12]`` — stable across processes,
restarts and machines, and the repository can be moved without invalidating
keys. A path outside the repository (rare) falls back to its absolute
lexical path.
"""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from config import path as config_path

from . import ownership

_FALLBACK_PREFIX = "session-"
_KEY_LENGTH = 12


@dataclass(frozen=True, slots=True)
class PlanIdentity:
    """A plan's physical storage identity.

    ``key`` names the directory under ``PLAN_KNOWLEDGE_DIR``; ``plan_name`` is
    the human-readable bare name used in payloads and legacy fallback reads.
    ``plan_ref`` is the canonical plan file path, or ``None`` for a
    session-derived fallback identity (no plan file resolves).
    """

    key: str
    plan_name: str
    plan_ref: Path | None = None

    @property
    def is_session_fallback(self) -> bool:
        """True when this identity has no canonical plan file."""
        return self.plan_ref is None


def _lexical_absolute(path: Path) -> Path:
    """Lexically normalize *path* (no symlink/filesystem access) to absolute form."""
    normalized = Path(os.path.normpath(str(path)))
    if not normalized.is_absolute():
        normalized = Path(os.path.normpath(str(config_path.ROOT_DIR / normalized)))
    return normalized


def plan_key(plan_ref: Path) -> str:
    """Stable storage key for a canonical plan path: ``sha1(rel path)[:12]``."""
    normalized = _lexical_absolute(plan_ref)
    try:
        seed = normalized.relative_to(config_path.ROOT_DIR).as_posix()
    except ValueError:
        seed = normalized.as_posix()
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:_KEY_LENGTH]


def fallback_plan_name(session_id: str) -> str:
    """Session-derived fallback plan name, hashed over the FULL session id."""
    digest = hashlib.sha1(session_id.encode("utf-8")).hexdigest()
    return f"{_FALLBACK_PREFIX}{digest[:8]}"


def fallback_plan_key(session_id: str) -> str:
    """Storage key for a session's fallback identity (no plan file resolves)."""
    return hashlib.sha1(f"session:{session_id}".encode()).hexdigest()[:_KEY_LENGTH]


def _normalize_ref(ref: str, anchor_session_id: str) -> Path | None:
    """Resolve one raw reference to a canonical, lexically normalized path.

    Existing files win (``config.path.resolve_plan_path`` — session-scoped
    first, then repo-relative). A missing
    reference is normalized syntactically so identity stays deterministic
    before the plan file is first written: a bare filename anchors to the
    anchor session's ``plans/`` directory; a path-shaped reference anchors to
    ``ROOT_DIR``; an absolute reference is kept.
    """
    raw = ref.strip()
    if not raw:
        return None
    anchor = anchor_session_id.strip()
    found = config_path.resolve_plan_path(raw, anchor or None)
    if found is not None:
        return _lexical_absolute(found)

    candidate = Path(raw)
    if candidate.is_absolute():
        return _lexical_absolute(candidate)
    if str(candidate.parent) in ("", "."):
        if not anchor:
            return None
        plans_dir = config_path.session_plans_dir(anchor)
        if plans_dir is None:
            return None
        filename = candidate.name if candidate.suffix else f"{candidate.name}.md"
        return _lexical_absolute(plans_dir / filename)
    return _lexical_absolute(config_path.ROOT_DIR / candidate)


def resolve_plan_identity(session_id: str, plan_name: str) -> PlanIdentity | None:
    """Resolve *plan_name* to this session's canonical plan identity, or ``None``.

    Candidates come from every association source whose raw ref carries the
    requested name. A single distinct path wins; several distinct paths are
    disambiguated by the session's own ``plan_ref`` (the most specific link) —
    without one the name is ambiguous and resolves to ``None`` (logged). When
    no path resolves at all, only the session's own fallback name is accepted.
    """
    sid = session_id.strip() if isinstance(session_id, str) else ""
    name = plan_name.strip() if isinstance(plan_name, str) else ""
    if not sid or not name:
        return None

    candidates: dict[str, Path] = {}
    preferred_state: Path | None = None
    for plan_ref in ownership.association_plan_refs(sid, name):
        normalized = _normalize_ref(plan_ref.ref, plan_ref.anchor_session_id)
        if normalized is None or normalized.stem != name:
            continue
        candidates.setdefault(str(normalized), normalized)
        if plan_ref.source == "state":
            preferred_state = normalized

    if len(candidates) == 1:
        selected = next(iter(candidates.values()))
    elif len(candidates) > 1 and preferred_state is not None:
        selected = preferred_state
    elif len(candidates) > 1:
        logger.warning(
            "knowledge identity: plan '{}' is ambiguous for session '{}' ({}); "
            "no session plan_ref disambiguates it",
            name,
            sid,
            sorted(candidates),
        )
        return None
    elif name == fallback_plan_name(sid):
        return PlanIdentity(key=fallback_plan_key(sid), plan_name=name, plan_ref=None)
    else:
        return None
    return PlanIdentity(key=plan_key(selected), plan_name=name, plan_ref=selected)


def resolve_session_plan_identity(session_id: str) -> PlanIdentity | None:
    """The session's primary identity: state ref first, todos, boulder, fallback.

    Used where a caller needs "the plan this session is working on" without
    naming it (the Tier-1 prompt block). Never ``None`` for a non-empty
    session id — the session-derived fallback identity is returned when no
    plan file resolves.
    """
    sid = session_id.strip() if isinstance(session_id, str) else ""
    if not sid:
        return None
    refs = ownership.association_plan_refs(sid)
    for source in ("state", "todos", "boulder"):
        for plan_ref in refs:
            if plan_ref.source != source:
                continue
            normalized = _normalize_ref(plan_ref.ref, plan_ref.anchor_session_id)
            if normalized is None:
                continue
            return PlanIdentity(
                key=plan_key(normalized), plan_name=normalized.stem, plan_ref=normalized
            )
    return PlanIdentity(
        key=fallback_plan_key(sid), plan_name=fallback_plan_name(sid), plan_ref=None
    )


def associated_plan_identities(session_id: str) -> list[PlanIdentity]:
    """Every identity this session may access (deduplicated, source order).

    The session-derived fallback identity is always included; identities whose
    directories hold no knowledge are filtered by the caller.
    """
    sid = session_id.strip() if isinstance(session_id, str) else ""
    if not sid:
        logger.debug("knowledge identity: empty session_id; no plan association")
        return []
    identities: dict[str, PlanIdentity] = {}
    for plan_ref in ownership.association_plan_refs(sid):
        normalized = _normalize_ref(plan_ref.ref, plan_ref.anchor_session_id)
        if normalized is None:
            continue
        identity = PlanIdentity(
            key=plan_key(normalized), plan_name=normalized.stem, plan_ref=normalized
        )
        identities.setdefault(identity.key, identity)
    fallback = PlanIdentity(
        key=fallback_plan_key(sid), plan_name=fallback_plan_name(sid), plan_ref=None
    )
    identities.setdefault(fallback.key, fallback)
    return list(identities.values())


def _other_session_plan_keys(session_id: str) -> set[str]:
    """Keys of plan paths a boulder work shares with a different session.

    Sharing is detected through boulder ``session_ids`` — the documented
    multi-session collaboration channel. Any work listing another session
    contributes the key of its canonical plan path; the removal side then
    never deletes knowledge another collaborator may still read.
    """
    boulder = ownership.read_boulder_document()
    if boulder is None:
        return set()

    keys: set[str] = set()
    for work in ownership.iter_boulder_works(boulder):
        if not isinstance(work, dict):
            continue
        session_ids = work.get("session_ids")
        if not isinstance(session_ids, list):
            continue
        listed = [item for item in session_ids if isinstance(item, str) and item.strip()]
        if not listed or all(item == session_id for item in listed):
            continue
        anchor = listed[0].strip()
        active_plan = work.get("active_plan")
        ref = (
            active_plan.strip()
            if isinstance(active_plan, str) and active_plan.strip()
            else str(work.get("plan_name") or "").strip()
        )
        normalized = _normalize_ref(ref, anchor)
        if normalized is not None:
            keys.add(plan_key(normalized))
    return keys


def private_plan_identities(session_id: str) -> list[PlanIdentity]:
    """This session's identities whose knowledge is not shared with another session.

    A plan identity is shared when a boulder work lists a different session and
    resolves to the same canonical plan path. The fallback identity is
    inherently private. Used by ``clear_session`` so collaboration survives a
    member's teardown.
    """
    sid = session_id.strip() if isinstance(session_id, str) else ""
    if not sid:
        return []
    shared_keys = _other_session_plan_keys(sid)
    return [
        identity
        for identity in associated_plan_identities(sid)
        if identity.plan_ref is None or identity.key not in shared_keys
    ]


__all__ = [
    "PlanIdentity",
    "associated_plan_identities",
    "fallback_plan_key",
    "fallback_plan_name",
    "plan_key",
    "private_plan_identities",
    "resolve_plan_identity",
    "resolve_session_plan_identity",
]
