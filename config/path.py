"""Filesystem path configuration (repo roots, data and skill directories)."""

import sys
from pathlib import Path

from dotenv import load_dotenv

from config.sherry_settings import get_sherry_setting

ROOT_DIR = Path(__file__).parent
ROOT_DIR = ROOT_DIR / ".."
ROOT_DIR = ROOT_DIR.resolve()

ENV_PATH = ROOT_DIR / ".env"
# Load environment variables early so the workspace template language below is
# read from the .env file (idempotent; existing environment variables win).
load_dotenv(ENV_PATH, override=False)

# The interpreter actually running this process (audit #32). Under every
# supported launch mode (./start.sh or `uv run python -m server`) this IS the
# project venv's python — on any platform (Windows ``Scripts\``, POSIX
# ``bin/``, conda, pyenv, system python), with no hardcoded venv layout.
# Helpers spawned as subprocesses (e.g. the STT daemon) must share the running
# environment's dependencies, so the running interpreter is the correct
# target everywhere.
INTERPRETER_PATH = Path(sys.executable)
CONTEXT_ENGINE_PATH = ROOT_DIR / "context_engine"
PLUGINS_PATH = ROOT_DIR / "plugins"

SRC_DIR = ROOT_DIR / "src"
STATIC_DIR = ROOT_DIR / "static"
TEMP_DIR = ROOT_DIR / "temp"

MODELS_DIR = ROOT_DIR / "models"
WORKSPACE_DIR = ROOT_DIR / "workspace"
WORKSPACE_TEMPLATE_DIR = WORKSPACE_DIR / "template"
KNOWLEDGE_DIR = WORKSPACE_DIR / "knowledge"
# Plan-extraction knowledge namespace (Tier-1 prompt injection + Tier-2 tool
# reads): one directory per plan, holding task-*.json / wave-*.json /
# plan-summary.json. Kept separate from KNOWLEDGE_INDEX_DIR (graph index).
PLAN_KNOWLEDGE_DIR = KNOWLEDGE_DIR / "plans"
MEMORY_DIR = WORKSPACE_DIR / "memory"
HEARTBEAT_PATH = WORKSPACE_DIR / "HEARTBEAT.md"
# The HEARTBEAT template is language-independent; it lives directly under the
# template dir (English text), NOT inside the locale subdirectories.
HEARTBEAT_TEMPLATE_PATH = WORKSPACE_TEMPLATE_DIR / "HEARTBEAT.md"
# Session-scoped working tree: each session owns {session_id}/ under the
# workspace (future home of {session_id}/plans/), keeping persona data in one root.
SESSIONS_DIR = WORKSPACE_DIR / "sessions"
SKILLS_DIR = ROOT_DIR / "skills"
AUTO_SKILLS_DIR = SKILLS_DIR / "auto/"
PLUGIN_SKILLS_DIR = SKILLS_DIR / "plugins"
SKILLS_STATE_FILE = PLUGIN_SKILLS_DIR / ".state.json"
# A SKILL.md is only discoverable when it lives under one of these top-level
# roots beneath SKILLS_DIR. A SKILL.md directly under skills/ or under an
# unexpected subdirectory is ignored by the loader and the skill index.
SKILL_DISCOVERY_ROOTS: tuple[str, ...] = ("builtin", "auto", "plugins")

# Additional directories
MEMORY_INDEX_DIR = MEMORY_DIR / "index"
KNOWLEDGE_INDEX_DIR = KNOWLEDGE_DIR / "index"

# i18n workspace templates (locale code -> subdirectory under WORKSPACE_TEMPLATE_DIR).
# Kept in sync with the client locales: en (default), zh, ja, ko.
WORKSPACE_TEMPLATE_LANGS: tuple[str, ...] = ("zh", "en", "ja", "ko")
# Fallback language used when a requested locale has no template directory.
# Configurable via WORKSPACE_TEMPLATE_LANG in the project-root sherry.jsonc.
DEFAULT_WORKSPACE_TEMPLATE_LANG = str(get_sherry_setting("WORKSPACE_TEMPLATE_LANG")).strip().lower()


def resolve_workspace_template_lang(lang: str | None = None) -> str:
    """Resolve a requested template language to an available locale code.

    Falls back to ``DEFAULT_WORKSPACE_TEMPLATE_LANG`` when ``lang`` is falsy,
    not one of the supported languages, or when the matching template
    subdirectory does not exist on disk.

    Args:
        lang: Requested language code, e.g. ``"en"``. ``None`` uses the default.

    Returns:
        A locale code from ``WORKSPACE_TEMPLATE_LANGS`` that has an existing
        template subdirectory (geometry guaranteed by the default fallback).
    """
    requested = (lang or DEFAULT_WORKSPACE_TEMPLATE_LANG).strip().lower()
    candidates = [requested] if requested != DEFAULT_WORKSPACE_TEMPLATE_LANG else [requested]
    candidates.append(DEFAULT_WORKSPACE_TEMPLATE_LANG)
    for code in candidates:
        if code in WORKSPACE_TEMPLATE_LANGS and (WORKSPACE_TEMPLATE_DIR / code).is_dir():
            return code
    return DEFAULT_WORKSPACE_TEMPLATE_LANG


def resolve_workspace_template_dir(lang: str | None = None) -> Path:
    """Return the template directory for ``lang``, falling back to a default."""
    return WORKSPACE_TEMPLATE_DIR / resolve_workspace_template_lang(lang)


def is_allowed_skill_path(skill_file: Path, skills_dir: Path | None = None) -> bool:
    """Return True when *skill_file* lives under an allowed skill root.

    ``skills_dir`` defaults to :data:`SKILLS_DIR`; callers that override the
    skills root (e.g. tests) pass it explicitly.
    """
    base = SKILLS_DIR if skills_dir is None else skills_dir
    try:
        rel = skill_file.relative_to(base)
    except ValueError:
        return False
    return bool(rel.parts) and rel.parts[0] in SKILL_DISCOVERY_ROOTS


# ── Plan / boulder path resolution (single source of truth) ────────────────
# Plans were migrated from repo-level ``.omo/plans/`` into each session tree
# (``workspace/sessions/<session_id>/plans/``). Legacy ``.omo`` references stay
# resolvable, and migrated files are found behind a legacy reference.
# ``ROOT_DIR`` / ``SESSIONS_DIR`` are read at call time so tests can repoint them.


def _omo_dir() -> Path:
    """Return the repo-root ``.omo`` orchestration directory (absolute)."""
    return ROOT_DIR / ".omo"


def is_safe_session_segment(session_id: str) -> bool:
    """Return True when *session_id* is a safe single path segment.

    Rejects empty, ``.`` / ``..``, and any value containing a path separator.
    Unlike :func:`pub.func.validator.session_id.is_safe_session_id`, this rule
    is path-root agnostic: plan directories anchor to ``SESSIONS_DIR``, not
    ``SRC_DIR``, so the containment check there does not apply.
    """
    if not session_id or session_id in (".", ".."):
        return False
    return "/" not in session_id and "\\" not in session_id


def session_plans_dir(session_id: str) -> Path | None:
    """Return the session's plan directory, or ``None`` for an unsafe id.

    The directory is ``SESSIONS_DIR/<session_id>/plans``. Unsafe session ids
    (empty / ``.`` / ``..`` / containing a path separator) yield ``None`` so
    callers fail open instead of escaping the sessions root.

    Note: ``clear_session`` removes ``SESSIONS_DIR/<session_id>/`` wholesale,
    so deleting a session also deletes its plans.
    """
    if not is_safe_session_segment(session_id):
        return None
    return SESSIONS_DIR / session_id / "plans"


def resolve_plan_path(plan_ref: str | Path | None, session_id: str | None = None) -> Path | None:
    """Resolve a plan reference to an existing file, or ``None``.

    Accepted forms, in priority order:

    1. **Session-scoped** — a bare filename (``x.md``) or any reference whose
       basename exists under ``SESSIONS_DIR/<session_id>/plans/``. This wins
       over the legacy copy when both exist, and also finds the migrated file
       behind a legacy ``.omo/plans/x.md`` reference.
    2. **Repo-relative** — ``ROOT_DIR / ref`` when that file exists; covers
       explicit new-form paths (``workspace/sessions/<id>/plans/x.md``) and the
       legacy ``.omo/plans/x.md`` layout.
    3. **Legacy basename** — ``ROOT_DIR/.omo/plans/<basename>`` (absolute
       references are accepted only when the file exists).

    The reference is never guessed: a missing file returns ``None``.
    """
    if plan_ref is None:
        return None
    ref = str(plan_ref).strip()
    if not ref:
        return None

    candidate = Path(ref)
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None

    if session_id:
        plans_dir = session_plans_dir(session_id)
        if plans_dir is not None:
            scoped = plans_dir / candidate.name
            if scoped.is_file():
                return scoped

    rooted = ROOT_DIR / candidate
    if rooted.is_file():
        return rooted

    legacy = _omo_dir() / "plans" / candidate.name
    if legacy.is_file():
        return legacy
    return None


def resolve_boulder_path() -> Path:
    """Return the absolute active-work pointer path (``ROOT_DIR/.omo/boulder.json``).

    ``boulder.json`` is written by the external orchestration layer, not by
    this repo; the repo only reads it.
    """
    return _omo_dir() / "boulder.json"


def resolve_evidence_ledger_path() -> Path:
    """Return the absolute evidence-ledger path (``ROOT_DIR/.omo/ledger.jsonl``).

    The ledger deliberately stays repo-scoped (not session-scoped): it is the
    append-only audit trail shared across sessions that verify the same plan.
    """
    return _omo_dir() / "ledger.jsonl"


def resolve_start_work_ledger_path() -> Path:
    """Return the absolute start-work ledger path (``ROOT_DIR/.omo/start-work/ledger.jsonl``)."""
    return _omo_dir() / "start-work" / "ledger.jsonl"
