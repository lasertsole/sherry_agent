from pathlib import Path

from config.path import resolve_workspace_template_dir
from config import WORKSPACE_DIR
from config.features import SERVER_HTTP
from workspace import ALL_SYSTEM_FILE_NAMES, COMMUNITY_SYSTEM_FILE_NAMES
from workspace.file_sync import ensure_workspace_system_files
from server.service.file_store import FileStore

# The user-editable persona files. AGENTS.md (the operating rules) is NOT in
# this list: it is written once by a built-in preset's creation path and is never
# exposed for editing afterwards. The agent-side anti-injection guards
# (agent/tools/memory.py, agent/security/threat_patterns.py) still refuse any
# "overwrite/modify AGENTS.md" content from tools.
# IDENTITY.md was removed entirely (dropped from ALL_SYSTEM_FILE_NAMES + templates),
# so it is absent from both the injection chain and this file whitelist.
EDITABLE_SYSTEM_FILE_NAMES = list(COMMUNITY_SYSTEM_FILE_NAMES)


class _WorkplaceFileStore(FileStore):
    """Persona/system prompt files at workspace/ root (template)."""

    def __init__(self) -> None:
        self.file_names = list(EDITABLE_SYSTEM_FILE_NAMES)
        self.max_content_length = SERVER_HTTP["system_prompt_max_content_length"]
        self.error_noun = "file"
        # Emptying a persona file is a legal state here, not an error: the
        # the Coding Assistant built-in preset ships an empty soul / user profile (no
        # role-play), and an empty file simply drops out of the assembled prompt.
        self.allow_empty = True

    def _before_read(self) -> None:
        # Lazy-ensure the persona files exist before reading (they may be deleted
        # from workspace/ root and copied in from template/ on first use).
        ensure_workspace_system_files()

    def _file_path(self, file_name: str) -> Path:
        return WORKSPACE_DIR / file_name


_store = _WorkplaceFileStore()


class _PresetFileStore(FileStore):
    """Writable store used ONLY by preset creation (AGENTS.md included).

    The ordinary persona API must not touch AGENTS.md; a built-in preset still
    composes its Operating Instructions from the shipped template, so creation
    needs one write path that accepts the core file. It is not exposed through
    any general-purpose edit route.
    """

    def __init__(self) -> None:
        self.file_names = list(ALL_SYSTEM_FILE_NAMES)
        self.max_content_length = SERVER_HTTP["system_prompt_max_content_length"]
        self.error_noun = "file"
        self.allow_empty = True

    def _before_read(self) -> None:
        ensure_workspace_system_files()

    def _file_path(self, file_name: str) -> Path:
        return WORKSPACE_DIR / file_name


_preset_store = _PresetFileStore()


def read_system_prompt_file() -> dict[str, str]:
    """Read every system prompt file (AGENTS.md included for template loading)."""
    return _preset_store.read_files()


def write_system_prompt_file(file_to_content: dict[str, str]) -> None:
    """Write user-editable persona files (AGENTS.md is refused)."""
    _store.write_files(file_to_content)


def update_system_prompt_file(file_to_content: dict[str, str]) -> None:
    """Update user-editable persona files (AGENTS.md is refused)."""
    _store.update_files(file_to_content)


def write_preset_system_prompt_file(file_to_content: dict[str, str]) -> None:
    """Write the full preset payload, AGENTS.md included (creation path only)."""
    _preset_store.write_files(file_to_content)


def read_system_prompt_template(lang: str | None = None) -> dict[str, str]:
    """Read system prompt template files for ``lang`` (default for the user's preferred language).

    Reads the persona template files from ``workspace/template/<lang>/`` and returns a
    ``file_name -> content`` map. Returns only files that exist in the resolved template dir.
    """
    template_dir = resolve_workspace_template_dir(lang)
    file_to_content: dict[str, str] = {}

    for file_name in ALL_SYSTEM_FILE_NAMES:
        file_path = template_dir / file_name
        if file_path.is_file():
            with open(file_path, encoding="utf-8") as file:
                file_to_content[file_name] = file.read()

    return file_to_content
