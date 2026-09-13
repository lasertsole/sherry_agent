from pathlib import Path

from config.path import resolve_workspace_template_dir
from config import WORKSPACE_DIR
from workspace import ALL_SYSTEM_FILE_NAMES
from workspace.file_sync import ensure_workspace_system_files
from server.service.file_store import FileStore

# AGENTS.md is injected into the system prompt by prompt_builder but is **not**
# editable through the /system_prompt API: the persona dialog UI no longer exposes
# it, and the backend must reject any write attempt (defense in depth). The template
# read is filtered the same way so the API can never leak/extend its edit surface.
# IDENTITY.md was removed entirely (dropped from ALL_SYSTEM_FILE_NAMES + templates),
# so it is absent from both the injection chain and this file whitelist.
EDITABLE_SYSTEM_FILE_NAMES = [name for name in ALL_SYSTEM_FILE_NAMES if name != "AGENTS.md"]


class _WorkplaceFileStore(FileStore):
    """Persona/system prompt files at workspace/ root (audit 2.1.7 template)."""

    def __init__(self) -> None:
        self.file_names = list(EDITABLE_SYSTEM_FILE_NAMES)
        self.max_content_length = 2_000
        self.error_noun = "file"

    def _before_read(self) -> None:
        # Lazy-ensure the persona files exist before reading (they may be deleted
        # from workspace/ root and copied in from template/ on first use).
        ensure_workspace_system_files()

    def _file_path(self, file_name: str) -> Path:
        return WORKSPACE_DIR / file_name


_store = _WorkplaceFileStore()


def read_system_prompt_file() -> dict[str, str]:
    """Read system prompt files"""
    return _store.read_files()


def write_system_prompt_file(file_to_content: dict[str, str]) -> None:
    """Write system prompt files"""
    _store.write_files(file_to_content)


def update_system_prompt_file(file_to_content: dict[str, str]) -> None:
    """Update system prompt files (only overwrite provided files, leave others unchanged)"""
    _store.update_files(file_to_content)


def read_system_prompt_template(lang: str | None = None) -> dict[str, str]:
    """Read system prompt template files for ``lang`` (default for the user's preferred language).

    Reads the persona template files from ``workspace/template/<lang>/`` and returns a
    ``file_name -> content`` map. Returns only files that exist in the resolved template dir.
    """
    template_dir = resolve_workspace_template_dir(lang)
    file_to_content: dict[str, str] = {}

    for file_name in EDITABLE_SYSTEM_FILE_NAMES:
        file_path = template_dir / file_name
        if file_path.is_file():
            with open(file_path, encoding="utf-8") as file:
                file_to_content[file_name] = file.read()

    return file_to_content
