from pathlib import Path

#: Directory names the walker refuses to descend into, at any depth. Dot-named
#: directories are skipped wholesale by :func:`should_skip_dir`, so only the
#: non-dot entries need naming here. Kept as a constant because the ripgrep
#: backend builds equivalent ``--glob`` exclusions from it — one list, two
#: engines, no drift.
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {"__pycache__", "node_modules", "venv", ".venv", ".git", ".hg", ".svn", ".idea", ".mypy_cache"}
)


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
    return name in SKIP_DIR_NAMES
