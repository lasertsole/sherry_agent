from config.path import MEMORY_DIR

# Memory directories (workspace/memory/). Only these file names are allowed to be
# read/written through the UI. USER.md overlaps with the workspace-root USER.md
# naming but lives in a separate directory (the long-term memory store).
MEMORY_SYSTEM_FILE_NAMES: list[str] = [
    "MEMORY.md",
    "USER.md",
]


# Memory entries are delimited by "\n§\n" on disk (see agent/tools/memory.py).
# The raw file is a plain text source; we treat it as a full-file editable text.
def read_memory_files() -> dict[str, str]:
    """Read memory files."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    file_to_content: dict[str, str] = {}

    for file_name in MEMORY_SYSTEM_FILE_NAMES:
        file_path = MEMORY_DIR / file_name
        if file_path.exists():
            with open(file_path, "r", encoding="utf-8") as file:
                file_to_content[file_name] = file.read()

    return file_to_content


def write_memory_files(file_to_content: dict[str, str]) -> None:
    """Write memory files (only provided files, leave others unchanged)."""
    existing = read_memory_files()

    for file_name, content in file_to_content.items():
        if file_name not in MEMORY_SYSTEM_FILE_NAMES:
            raise ValueError(f"Invalid memory file name: {file_name}")
        elif not isinstance(content, str):
            raise ValueError(f"Invalid content type for memory file: {file_name}")
        elif len(content.strip()) == 0:
            raise ValueError(f"Content is empty for memory file: {file_name}")
        elif len(content) > 8_000:
            raise ValueError(f"Content too long for memory file: {file_name}")

        existing[file_name] = content

    for file_name, content in existing.items():
        file_path = MEMORY_DIR / file_name
        with open(file_path, "w", encoding="utf-8") as file:
            file.write(content)
