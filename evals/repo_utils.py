"""Small git helpers shared by the host-side eval harnesses."""

from __future__ import annotations

from pathlib import Path

__all__ = ["ensure_repo_excludes"]


def ensure_repo_excludes(repo_dir: Path, *patterns: str) -> None:
    """Add local-only ignores to a disposable eval clone.

    The agent's private ``.sherry`` state must never make the checkout look
    dirty or be walked by ``git clean``; ``.git/info/exclude`` is per-clone, so
    the benchmark checkout is unaffected beyond its own directory.
    """
    info = repo_dir / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    exclude = info / "exclude"
    existing = exclude.read_text(encoding="utf-8").splitlines() if exclude.is_file() else []
    missing = [pattern for pattern in patterns if pattern not in existing]
    if missing:
        exclude.write_text("\n".join([*existing, *missing]) + "\n", encoding="utf-8")
