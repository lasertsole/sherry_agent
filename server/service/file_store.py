"""Shared read/validate/write file-store template (audit 2.1.7).

Three UI-editable text file stores previously implemented the same
``read_*_file()`` (iterate allowed names, read what exists) and
``write_*_file()`` (validate name/type/non-empty/length, write) patterns:
``server/service/workplace.py``, ``server/service/memory.py`` and
``server/service/heartbeat.py``. The template lives here; each service keeps
its module-level function names and delegates to a store instance.

Per-store behavior differences are preserved via the subclass knobs:

- ``error_noun`` pins the exact validation-error wording
  (``"file"`` / ``"memory file"`` / ``"heartbeat file"``);
- ``_content_length`` pins the length budget (heartbeat counts task text only);
- ``_file_path`` resolves each allowed name at call time (so monkeypatched
  config paths keep working);
- validation and writing are interleaved per entry in :meth:`write_files`
  (an invalid later entry does not undo earlier writes — matching the
  original per-entry loops);
- the merge-then-write-back variants keep their original re-validation
  semantics: workplace's update re-validates everything via
  :meth:`write_files`, memory's write writes merged content back raw.
"""

from pathlib import Path


class FileStore:
    file_names: list[str]
    max_content_length: int
    error_noun: str = "file"

    def _before_read(self) -> None:
        """Optional pre-read hook (ensure persona files exist / mkdir)."""

    def _content_length(self, content: str) -> int:
        return len(content)

    def _file_path(self, file_name: str) -> Path:
        raise NotImplementedError

    def _validate(self, file_name: str, content) -> None:
        if file_name not in self.file_names:
            raise ValueError(f"Invalid {self.error_noun} name: {file_name}")
        elif not isinstance(content, str):
            raise ValueError(f"Invalid content type for {self.error_noun}: {file_name}")
        elif len(content.strip()) == 0:
            raise ValueError(f"Content is empty for {self.error_noun}: {file_name}")
        elif self._content_length(content) > self.max_content_length:
            raise ValueError(f"Content too long for {self.error_noun}: {file_name}")

    def read_files(self) -> dict[str, str]:
        self._before_read()
        file_to_content: dict[str, str] = {}
        for file_name in self.file_names:
            file_path = self._file_path(file_name)
            if file_path.exists():
                with open(file_path, "r", encoding="utf-8") as file:
                    file_to_content[file_name] = file.read()
        return file_to_content

    def _write_one(self, file_name: str, content: str) -> None:
        file_path = self._file_path(file_name)
        with open(file_path, "w", encoding="utf-8") as file:
            file.write(content)

    def _write_entries(self, file_to_content: dict[str, str]) -> None:
        for file_name, content in file_to_content.items():
            self._write_one(file_name, content)

    def write_files(self, file_to_content: dict[str, str]) -> None:
        """Validate and write the provided entries (only them), interleaved."""
        for file_name, content in file_to_content.items():
            self._validate(file_name, content)
            self._write_one(file_name, content)

    def update_files(self, file_to_content: dict[str, str]) -> None:
        """Read current files, validate+merge the provided entries, write back.

        The write-back re-validates every entry (workplace semantics: the
        merged map goes through :meth:`write_files`).
        """
        existing = self.read_files()
        for file_name, content in file_to_content.items():
            self._validate(file_name, content)
            existing[file_name] = content
        self.write_files(existing)

    def merge_files(self, file_to_content: dict[str, str]) -> None:
        """Read current files, validate+merge the provided entries, write back.

        The write-back does NOT re-validate existing content (memory
        semantics: only the provided entries pass through ``_validate``).
        """
        existing = self.read_files()
        for file_name, content in file_to_content.items():
            self._validate(file_name, content)
            existing[file_name] = content
        self._write_entries(existing)
