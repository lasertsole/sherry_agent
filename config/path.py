"""Filesystem path configuration (repo roots, data and skill directories)."""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from config.sherry_settings import get_sherry_setting

ROOT_DIR = Path(__file__).parent
ROOT_DIR = ROOT_DIR / ".."
ROOT_DIR = ROOT_DIR.resolve()

#: The WRITABLE app-data root. Unset (a checkout, a server, CI) keeps every
#: directory below exactly where it always was — ``ROOT_DIR`` — so nothing
#: changes for existing installs. A PACKAGED desktop build sets it (the Tauri
#: shell points it at the OS app-data dir) because the code tree ships
#: read-only: sessions, memory, uploads, logs and the ``.env`` must live
#: somewhere the user can write, and must survive an app update.
APP_HOME = (
    Path(os.environ.get("SHERRY_HOME", "")).expanduser().resolve()
    if os.environ.get("SHERRY_HOME", "").strip()
    else ROOT_DIR
)

ENV_PATH = APP_HOME / ".env"
if APP_HOME != ROOT_DIR and not ENV_PATH.exists():
    # Seed the editable copy from the TRACKED .env (it is the shipped default:
    # the file holds the non-secret application configuration only — model and
    # network credentials live in .models / .network).
    template = ROOT_DIR / ".env"
    if template.exists():
        try:
            ENV_PATH.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
# The model configuration (.models) and the network service configuration
# (.network) live in their own project-root files (config/models_settings.py,
# config/network_settings.py). Importing them seeds the files when missing AND
# materialises their values into os.environ. They are imported BEFORE the
# environment file is loaded on purpose: the hydration never overwrites a key
# that is already set, so writing them FIRST makes the new files outrank a
# legacy .env entry — while an operator's own environment variable (set before
# the process started) still outranks both.
from config.models_settings import hydrate_model_env  # noqa: E402
from config.network_settings import hydrate_network_env  # noqa: E402

# Load environment variables early so the workspace template language below is
# read from the .env file (idempotent; already-set variables win, which is what
# shields the hydrated model / network values from a legacy .env line).
load_dotenv(ENV_PATH, override=False)


def load_env(override: bool = False) -> None:
    """(Re)apply the configuration stack: ``.models`` / ``.network``, then ``.env``.

    The replacement for bare ``load_dotenv(ENV_PATH, override=True)`` calls.
    The ORDER defines precedence: the hydrations write the new files' values
    first (skipping anything already set, which protects explicit process
    variables), and the environment file then fills only the keys still unset —
    a legacy ``.env`` copy of a moved key can never win. Runtime edits to ``.env``
    are likewise not re-applied for an already-set key (the moved keys are
    boot-read anyway); restart the process to pick one up.

    ``override=True`` is available for a caller that wants the environment file
    to win over the new files; nothing in the app does.
    """
    hydrate_model_env()
    hydrate_network_env()
    load_dotenv(ENV_PATH, override=override)


# The interpreter actually running this process. Under every
# supported launch mode (./start.sh or `uv run python -m server`) this IS the
# project venv's python — on any platform (Windows ``Scripts\``, POSIX
# ``bin/``, conda, pyenv, system python), with no hardcoded venv layout.
# Helpers spawned as subprocesses (e.g. the STT daemon) must share the running
# environment's dependencies, so the running interpreter is the correct
# target everywhere.
INTERPRETER_PATH = Path(sys.executable)
CONTEXT_ENGINE_PATH = ROOT_DIR / "context_engine"
PLUGINS_PATH = ROOT_DIR / "plugins"

SRC_DIR = APP_HOME / "src"
STATIC_DIR = APP_HOME / "static"
TEMP_DIR = APP_HOME / "temp"

MODELS_DIR = APP_HOME / "models"
WORKSPACE_DIR = APP_HOME / "workspace"
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
#: Shipped skills (read-only in a packaged build).
SKILLS_DIR = ROOT_DIR / "skills"
#: Where the agent WRITES skills (curator / uploads / enable-state). In a
#: checkout this IS ``SKILLS_DIR``; a packaged build points it into APP_HOME so
#: learned and uploaded skills persist across updates. The loader scans BOTH
#: roots (``skills_loader_roots()``).
USER_SKILLS_DIR = APP_HOME / "skills"
AUTO_SKILLS_DIR = USER_SKILLS_DIR / "auto/"
PLUGIN_SKILLS_DIR = USER_SKILLS_DIR / "plugins"
SKILLS_STATE_FILE = PLUGIN_SKILLS_DIR / ".state.json"
#: Loader cache (written on every scan) — writable by definition.
SKILLS_SNAPSHOT_FILE = USER_SKILLS_DIR / "skills_snapshot.json"
# A SKILL.md is only discoverable when it lives under one of these top-level
# roots beneath SKILLS_DIR. A SKILL.md directly under skills/ or under an
# unexpected subdirectory is ignored by the loader and the skill index.
SKILL_DISCOVERY_ROOTS: tuple[str, ...] = ("builtin", "auto", "plugins")

# Additional directories
MEMORY_INDEX_DIR = MEMORY_DIR / "index"
KNOWLEDGE_INDEX_DIR = KNOWLEDGE_DIR / "index"

# Code intelligence symbol index (subagent-only tree-sitter tooling). The
# directory is gitignored like .codegraph/; the SQLite file inside is created
# lazily on first index build.
CODE_INTEL_DIR = APP_HOME / ".codeintel"

#: Log tree (per-type sub-directories live under it).
LOG_DIR = APP_HOME / "logs" / "output"


def app_relative(path: Path) -> Path:
    """The path relative to whichever home owns it (APP_HOME or ROOT_DIR).

    User data lives under APP_HOME in a packaged build and under ROOT_DIR in a
    checkout, so a caller that wants a short display path must not assume one
    root: this returns the first relationship that holds, and the path itself
    when neither does (a caller must never crash on a display string).
    """
    for home in (APP_HOME, ROOT_DIR):
        try:
            return path.relative_to(home)
        except ValueError:
            continue
    return path


def resolve_app_path(relative: str | Path) -> Path:
    """Map an app-relative path (the shape the skills loader reports) to a file.

    Both skill trees report the CHECKOUT shape (``skills/auto/…``) no matter
    which home they live in, so a reader asks here: the file under APP_HOME
    wins when it exists, else the shipped one under ROOT_DIR. In a checkout the
    two are the same tree and this is a plain join.
    """
    rel = Path(str(relative).lstrip("./"))
    candidate = APP_HOME / rel
    if candidate.exists():
        return candidate
    return ROOT_DIR / rel


def skills_loader_roots() -> tuple[Path, ...]:
    """Every root the skill loader scans, deduplicated.

    A checkout has one root; a packaged build has two — the shipped ``builtin``
    tree (read-only) and the writable ``auto`` / ``plugins`` tree under
    APP_HOME.
    """
    if USER_SKILLS_DIR == SKILLS_DIR:
        return (SKILLS_DIR,)
    return (SKILLS_DIR, USER_SKILLS_DIR)


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
    # Requested first, then the default — deduplicated (the old ternary had both
    # branches identical, so a requested value equal to the default was tried
    # twice).
    candidates = [requested]
    if requested != DEFAULT_WORKSPACE_TEMPLATE_LANG:
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
# Plans live in each session tree (``workspace/sessions/<session_id>/plans/``).
# Active-work state and the evidence ledger live under ``src/data/``. These
# locations are Sherry-owned by contract: no external orchestration directory
# participates in plan, boulder or ledger resolution.
# ``ROOT_DIR`` / ``SESSIONS_DIR`` / ``SRC_DIR`` are read at call time so tests
# can repoint them.


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


def _is_plain_relative_ref(candidate: Path) -> bool:
    """True when a relative plan reference may be rooted at ``ROOT_DIR``.

    Plans are user-visible workspace artifacts: a reference that enters a
    hidden (dot-prefixed) tooling directory, or traverses upwards, never names
    a plan file and must not resolve.
    """
    parts = candidate.parts
    if not parts or ".." in parts:
        return False
    return not parts[0].startswith(".")


def resolve_plan_path(plan_ref: str | Path | None, session_id: str | None = None) -> Path | None:
    """Resolve a plan reference to an existing file, or ``None``.

    Accepted forms, in priority order:

    1. **Session-scoped** — a bare filename (``x.md``) or any reference whose
       basename exists under ``SESSIONS_DIR/<session_id>/plans/``.
    2. **Repo-relative** — ``ROOT_DIR / ref`` when that file exists and the
       reference is a plain (non-hidden, non-traversing) path; covers explicit
       paths (``workspace/sessions/<id>/plans/x.md``). References into hidden
       tooling directories or upwards out of the repo never resolve.

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
    if _is_plain_relative_ref(candidate) and rooted.is_file():
        return rooted

    return None


def resolve_boulder_path() -> Path:
    """Return the absolute active-work pointer path (``SRC_DIR/data/boulder.json``).

    The pointer records the plan work the execution protocol is driving. A
    missing file means "no active work" — every reader treats it as a safe
    default, never as an error.
    """
    return SRC_DIR / "data" / "boulder.json"


def resolve_evidence_ledger_path() -> Path:
    """Return the absolute evidence-ledger path (``SRC_DIR/data/evidence-ledger.jsonl``).

    The ledger deliberately stays repo-scoped (not session-scoped): it is the
    append-only audit trail shared across sessions that verify the same plan.
    The ``data`` directory is created on the ledger's first append.
    """
    return SRC_DIR / "data" / "evidence-ledger.jsonl"


#: Warn-once guard for an invalid configured project directory (see
#: :func:`resolve_default_project_dir`). Tool calls resolve the default on the
#: cold path, so a bad value would otherwise log per call.
_warned_invalid_project_dir = False


def validate_project_dir(value: str | os.PathLike[str]) -> Path:
    """Validate a project directory supplied by an operator or a session.

    Returns the resolved absolute directory. Raises ``ValueError`` when the
    value is empty, relative (there is no stable base to resolve it against),
    missing, unreadable, not a directory, or a symlink loop — ``Path.resolve``
    stops silently on loops, so they are surfaced here as an error instead of
    resolving to something unexpected.

    Deliberately strict for *input* validation; :func:`resolve_default_project_dir`
    wraps it with the always-boot fallback used at startup.
    """
    raw = str(value).strip()
    if not raw:
        raise ValueError("project directory is empty")
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        raise ValueError(f"project directory must be an absolute path: {raw!r}")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise ValueError(f"project directory cannot be resolved: {raw!r} ({e})") from e
    if not resolved.is_dir():
        raise ValueError(f"project directory is not a directory: {raw!r}")
    return resolved


def resolve_default_project_dir() -> Path:
    """The process-level default project directory.

    Precedence: ``SHERRY_PROJECT_DIR`` (read at call time — tests and operators
    repoint it, the same contract as :func:`resolve_approval_store_path`) →
    the ``project_dir`` key of ``sherry.jsonc`` → ``ROOT_DIR``. The final
    fallback keeps the pre-project-binding behaviour byte-for-byte.

    A configured value that fails :func:`validate_project_dir` never raises
    here — the server must always boot — but the fallback is *visible*: one
    warning per process naming the bad value, so a typo cannot silently leave
    the agent working inside the sherry checkout.
    """
    global _warned_invalid_project_dir
    configured = os.environ.get("SHERRY_PROJECT_DIR", "").strip()
    source = "SHERRY_PROJECT_DIR"
    if not configured:
        configured = str(get_sherry_setting("project_dir") or "").strip()
        source = "sherry.jsonc project_dir"
    if not configured:
        return ROOT_DIR
    try:
        return validate_project_dir(configured)
    except ValueError as e:
        if not _warned_invalid_project_dir:
            _warned_invalid_project_dir = True
            from loguru import logger

            logger.warning(
                "Invalid {} value ignored (falling back to the repository root {}): {}",
                source,
                ROOT_DIR,
                e,
            )
        return ROOT_DIR


def resolve_approval_store_path() -> Path:
    """Return the absolute tool-approval store path (``SRC_DIR/data/approvals.json``).

    The store is Sherry-owned runtime state — the same repo-scoped ``src/data``
    tree as the boulder pointer and the evidence ledger — so it never leaks into
    the tracked persona workspace. ``SRC_DIR`` is read at call time (tests
    repoint it), and the ``SHERRY_APPROVAL_STORE_PATH`` environment variable
    overrides the location outright (tests and operators). A missing file means
    "no recorded decisions", never an error.
    """
    override = os.environ.get("SHERRY_APPROVAL_STORE_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return SRC_DIR / "data" / "approvals.json"


def file_locks_dir() -> Path:
    """Return the cross-process file-lock directory (``SRC_DIR/data/locks``).

    The file tools' advisory ``flock`` files live here — one per canonical
    target path — instead of beside the sources, so a project tree never gains
    lock-file litter. ``SRC_DIR`` is read at call time (tests repoint it), and
    ``SHERRY_FILE_LOCKS_DIR`` overrides the location outright (tests need a
    scratch directory per case). The lock helper creates the directory on
    demand; nothing needs to pre-create it.
    """
    override = os.environ.get("SHERRY_FILE_LOCKS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return SRC_DIR / "data" / "locks"


def isolated_workspaces_dir() -> Path:
    """Return the isolated-workspace directory (``SRC_DIR/data/isolated``).

    An isolated subagent works in ``<here>/<run slug>/tree`` — a copy of the
    parent's project directory — and the merge reads it back on completion.
    Keeping it under ``src/data`` means an isolation copy never lands inside the
    user's project. ``SRC_DIR`` is read at call time (tests repoint it), and
    ``SHERRY_ISOLATED_WORKSPACES_DIR`` overrides the location outright.
    """
    override = os.environ.get("SHERRY_ISOLATED_WORKSPACES_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return SRC_DIR / "data" / "isolated"
