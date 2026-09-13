from pathlib import Path

from config.features import SERVER_HTTP
from config.path import MEMORY_DIR
from server.service.file_store import FileStore

# Memory directories (workspace/memory/). Only these file names are allowed to be
# read/written through the UI. USER.md overlaps with the workspace-root USER.md
# naming but lives in a separate directory (the long-term memory store).
MEMORY_SYSTEM_FILE_NAMES: list[str] = SERVER_HTTP["memory_system_file_names"]


class _MemoryFileStore(FileStore):
    """Long-term memory files at workspace/memory/ (audit 2.1.7 template).

    Writes merge into the current files and write everything back WITHOUT
    re-validating existing content (the original ``write_memory_files``
    validated only the provided entries).
    """

    def __init__(self) -> None:
        self.file_names = list(MEMORY_SYSTEM_FILE_NAMES)
        self.max_content_length = 8_000
        self.error_noun = "memory file"

    def _before_read(self) -> None:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    def _file_path(self, file_name: str) -> Path:
        return MEMORY_DIR / file_name


_store = _MemoryFileStore()


def read_memory_files() -> dict[str, str]:
    """Read memory files."""
    return _store.read_files()


def write_memory_files(file_to_content: dict[str, str]) -> None:
    """Write memory files (only provided files, leave others unchanged)."""
    _store.merge_files(file_to_content)
