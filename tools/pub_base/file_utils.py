from pathlib import Path


def is_text_file(path: Path, sample_size: int = 4096) -> bool:
    """Check if a file appears to be a text file (no null bytes in sample)."""
    try:
        with path.open("rb") as f:
            chunk = f.read(sample_size)
        if b"\x00" in chunk:
            return False
        return True
    except (OSError, PermissionError):
        return False


def should_skip_dir(d: Path) -> bool:
    """Return True for directories that should be skipped during file walking."""
    name = d.name
    if name.startswith(".") and name not in (".", ".."):
        return True
    if name in ("__pycache__", "node_modules", ".venv", "venv", ".git", ".hg", ".svn", ".idea", ".mypy_cache"):
        return True
    return False