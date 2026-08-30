import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent
ROOT_DIR = ROOT_DIR / ".."
ROOT_DIR = ROOT_DIR.resolve()

ENV_PATH = ROOT_DIR / ".env"
# Load environment variables early so the workspace template language below is
# read from the .env file (idempotent; existing environment variables win).
load_dotenv(ENV_PATH, override=False)

INTERPRETER_PATH = ROOT_DIR / ".venv/Scripts/python"
CONTEXT_ENGINE_PATH = ROOT_DIR / "context_engine"
PLUGINS_PATH = ROOT_DIR / "plugins"

SRC_DIR = ROOT_DIR / "src"
STATIC_DIR = ROOT_DIR / "static"
TEMP_DIR = ROOT_DIR / "temp"

MODELS_DIR = ROOT_DIR / "models"
SESSIONS_DIR = ROOT_DIR / "sessions"
SKILLS_DIR = ROOT_DIR / "skills"
AUTO_SKILLS_DIR = SKILLS_DIR / "auto/"
PLUGIN_SKILLS_DIR = SKILLS_DIR / "plugins"
SKILLS_STATE_FILE = PLUGIN_SKILLS_DIR / ".state.json"
WORKSPACE_DIR = ROOT_DIR / "workspace"
WORKSPACE_TEMPLATE_DIR = WORKSPACE_DIR / "template"
KNOWLEDGE_DIR = WORKSPACE_DIR / "knowledge"
MEMORY_DIR = WORKSPACE_DIR / "memory"
HEARTBEAT_PATH = WORKSPACE_DIR / "HEARTBEAT.md"
# The HEARTBEAT template is language-independent; it lives directly under the
# template dir (English text), NOT inside the locale subdirectories.
HEARTBEAT_TEMPLATE_PATH = WORKSPACE_TEMPLATE_DIR / "HEARTBEAT.md"

# Additional directories
MEMORY_INDEX_DIR = MEMORY_DIR / "index"
KNOWLEDGE_INDEX_DIR = KNOWLEDGE_DIR / "index"

# i18n workspace templates (locale code -> subdirectory under WORKSPACE_TEMPLATE_DIR).
# Kept in sync with the client locales: en (default), zh, ja, ko.
WORKSPACE_TEMPLATE_LANGS: tuple[str, ...] = ("zh", "en", "ja", "ko")
# Fallback language used when a requested locale has no template directory.
# Configurable via the WORKSPACE_TEMPLATE_LANG environment variable (.env).
DEFAULT_WORKSPACE_TEMPLATE_LANG = os.getenv("WORKSPACE_TEMPLATE_LANG", "en").strip().lower()


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
