"""Shared path resolution utilities for file tools."""
import os
from pathlib import Path

from config import ROOT_DIR


def resolve_path(file_path: str) -> Path:
    """Resolve file_path against ROOT_DIR if relative; expand ~."""
    p = Path(os.path.expanduser(file_path))
    if not p.is_absolute():
        p = ROOT_DIR / p
    return p.resolve()
