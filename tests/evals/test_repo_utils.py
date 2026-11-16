"""Shared eval git helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.repo_utils import ensure_repo_excludes

pytestmark = [pytest.mark.unit]


def _git_repo(tmp_path: Path) -> Path:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "e@x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "e"], cwd=repo, check=True)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def test_repo_exclude_is_local_and_idempotent(tmp_path: Path):
    repo = _git_repo(tmp_path)
    ensure_repo_excludes(repo, ".sherry/")
    ensure_repo_excludes(repo, ".sherry/")

    text = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert text.splitlines().count(".sherry/") == 1
    assert not (repo / ".gitignore").exists()
