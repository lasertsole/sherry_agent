from pathlib import Path

from config.features import SERVER_HTTP
from config.path import MEMORY_DIR
from server.service.file_store import FileStore

# Memory directories (workspace/memory/). Only these file names are allowed to be
# read/written through the UI/API. USER.md overlaps with the workspace-root USER.md
# naming but lives in a separate directory (the long-term memory store). FACTS.md
# is the one per-project entry: it resolves under the requesting session's
# `<project>/.sherry/memory/` (global fallback) while MEMORY/USER stay global.
MEMORY_SYSTEM_FILE_NAMES: list[str] = SERVER_HTTP["memory_system_file_names"]


def _read_session_root(session_id: str) -> Path | None:
    """The session's bound project root (durable tier), or ``None``."""
    if not session_id:
        return None
    from runtime.session.project_dir import read_project_dir_durable

    return read_project_dir_durable(session_id)


class _MemoryFileStore(FileStore):
    """Long-term memory files at workspace/memory/ (template).

    Writes merge into the current files and write everything back WITHOUT
    re-validating existing content (the original ``write_memory_files``
    validated only the provided entries).

    FACTS.md is per-project: with a ``session_id`` whose session is bound to a
    project directory, it resolves to ``<project>/.sherry/memory/FACTS.md``;
    without one (or for an unbound session) it stays in the global directory.
    MEMORY.md / USER.md are always global.
    """

    def __init__(self, session_id: str | None = None) -> None:
        self.file_names = list(MEMORY_SYSTEM_FILE_NAMES)
        self.max_content_length = 8_000
        self.error_noun = "memory file"
        self.session_id = str(session_id or "").strip()
        self._project_root_resolved = False
        self._project_root: Path | None = None

    def _before_read(self) -> None:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)

    def _resolve_project_root(self) -> Path | None:
        if not self._project_root_resolved:
            self._project_root_resolved = True
            if self.session_id:
                self._project_root = _read_session_root(self.session_id)
        return self._project_root

    def _file_path(self, file_name: str) -> Path:
        if file_name == "FACTS.md":
            root = self._resolve_project_root()
            if root is not None:
                return root / ".sherry" / "memory" / "FACTS.md"
        return MEMORY_DIR / file_name

    def _write_one(self, file_name: str, content: str) -> None:
        file_path = self._file_path(file_name)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as file:
            file.write(content)


def read_memory_files(session_id: str | None = None) -> dict[str, str]:
    """Read memory files (project FACTS when ``session_id`` is bound)."""
    return _MemoryFileStore(session_id).read_files()


def write_memory_files(file_to_content: dict[str, str], session_id: str | None = None) -> None:
    """Write memory files (only provided files, leave others unchanged)."""
    _MemoryFileStore(session_id).merge_files(file_to_content)
