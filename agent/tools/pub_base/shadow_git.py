"""Per-project shadow git snapshots.

Every sherry write takes a snapshot in ``<project>/.sherry/shadow-git`` using
``GIT_DIR``/``GIT_WORK_TREE``/``GIT_INDEX_FILE`` isolation. The user's ``.git``
is never initialized, staged, committed, reset, checked out or used for a
worktree. Snapshots are fail-open: a broken shadow store never blocks a write.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from loguru import logger

from config.features import SHADOW_GIT, SUBAGENT_ISOLATION
from pub.git_head import SHADOW_REF, shadow_head

__all__ = [
    "SHADOW_REF",
    "ensure_checkpoint",
    "list_checkpoints",
    "list_restorable_files",
    "new_turn",
    "read_commit_diff",
    "read_commit_files",
    "restore_checkpoint",
    "run_shadow",
    "shadow_env",
    "shadow_head",
    "shadow_store",
    "take_snapshot",
]

_COMMIT_MSG_PREFIX = "sherry|"

#: (session, root) pairs already snapshotted in the current turn.
_dedup: set[tuple[str, str]] = set()
_turn_counts: dict[str, int] = {}


def shadow_store(project_root: Path) -> Path:
    return project_root.resolve() / ".sherry" / "shadow-git"


def _ensure_store(project_root: Path) -> Path:
    store = shadow_store(project_root)
    if not (store / "HEAD").exists():
        store.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--bare", "-q", str(store)],
            capture_output=True,
            timeout=15,
        )
        subprocess.run(
            ["git", "--git-dir", str(store), "symbolic-ref", "HEAD", SHADOW_REF],
            capture_output=True,
            timeout=10,
        )
    info = store / "info"
    info.mkdir(parents=True, exist_ok=True)
    exclude = info / "exclude"
    patterns = [
        ".git/",
        ".sherry/shadow-git/",
        *SUBAGENT_ISOLATION["deny_patterns"],
    ]
    exclude.write_text(
        "# sherry shadow-git: never snapshotted (secrets, bulk, repository state)\n"
        + "\n".join(patterns)
        + "\n",
        encoding="utf-8",
    )
    return store


def shadow_env(project_root: Path) -> dict[str, str]:
    root = project_root.resolve()
    store = shadow_store(root)
    return {
        "GIT_DIR": str(store),
        "GIT_WORK_TREE": str(root),
        "GIT_INDEX_FILE": str(store / "index"),
        "GIT_AUTHOR_NAME": "sherry",
        "GIT_AUTHOR_EMAIL": "sherry@localhost",
        "GIT_COMMITTER_NAME": "sherry",
        "GIT_COMMITTER_EMAIL": "sherry@localhost",
    }


def run_shadow(
    project_root: Path, args: list[str], *, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    _ensure_store(project_root)
    env = {**os.environ, **shadow_env(project_root)}
    return subprocess.run(
        ["git", *args],
        cwd=str(project_root.resolve()),
        env=env,
        capture_output=True,
        text=True,
        timeout=SHADOW_GIT["git_timeout_s"] if timeout is None else timeout,
    )


def _current_turn(session_id: str) -> int:
    return _turn_counts.get(session_id, 0)


def _format_message(reason: str, session_id: str) -> str:
    parts = reason.split(":", 2)
    cp_type = parts[0] if parts else "unknown"
    tool = ""
    target = ""
    if cp_type == "before_write" and len(parts) == 3:
        tool, target = parts[1], parts[2]
    elif cp_type in {"pre_restore", "manual"}:
        target = parts[1] if len(parts) > 1 else ""
    else:
        target = reason
    target = target.replace("|", "/")[:300]
    return (
        f"{_COMMIT_MSG_PREFIX}turn={_current_turn(session_id)}|type={cp_type}"
        f"|tool={tool}|target={target}"
    )


def _parse_message(message: str) -> dict[str, str]:
    if not message.startswith(_COMMIT_MSG_PREFIX):
        return {"type": "unknown", "raw": message}
    fields: dict[str, str] = {}
    for part in message.split("|")[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key] = value
    return fields


def _summary(fields: dict[str, str]) -> str:
    cp_type = fields.get("type", "unknown")
    target = fields.get("target", "")
    tool = fields.get("tool", "")
    if cp_type == "before_write":
        return f"before {tool} {target}".strip()
    if cp_type == "pre_restore":
        return "pre-restore (undo point)"
    if cp_type == "manual":
        return f"manual: {target}".strip()
    return str(fields.get("raw", "checkpoint"))


def _prune_snapshots(project_root: Path) -> None:
    count = run_shadow(project_root, ["rev-list", "--count", SHADOW_REF], timeout=10)
    try:
        total = int(count.stdout.strip())
    except ValueError:
        return
    keep = int(SHADOW_GIT["max_snapshots"])
    if total <= keep:
        return
    oldest = run_shadow(
        project_root,
        ["rev-list", f"--skip={total - keep}", "-n1", SHADOW_REF],
        timeout=10,
    )
    tip = oldest.stdout.strip()
    if tip:
        run_shadow(project_root, ["update-ref", SHADOW_REF, tip], timeout=10)


def take_snapshot(project_root: Path, reason: str, *, session_id: str = "") -> str | None:
    """Take one snapshot and return its commit hash (None on failure)."""
    root = project_root.resolve()
    _ensure_store(root)
    add = run_shadow(root, ["add", "-A"])
    if add.returncode != 0:
        raise RuntimeError((add.stderr or add.stdout or "git add failed").strip()[:300])
    tree = run_shadow(root, ["write-tree"], timeout=10).stdout.strip()
    if not tree:
        raise RuntimeError("git write-tree returned no tree")
    parent = run_shadow(root, ["rev-parse", "--verify", SHADOW_REF], timeout=10)
    parent_args = ["-p", parent.stdout.strip()] if parent.returncode == 0 else []
    message = _format_message(reason, session_id)
    commit = run_shadow(
        root,
        ["commit-tree", tree, *parent_args, "-m", message],
        timeout=10,
    )
    sha = commit.stdout.strip()
    if commit.returncode != 0 or not sha:
        raise RuntimeError((commit.stderr or "git commit-tree failed").strip()[:300])
    update = run_shadow(root, ["update-ref", SHADOW_REF, sha], timeout=10)
    if update.returncode != 0:
        raise RuntimeError((update.stderr or "git update-ref failed").strip()[:300])
    _prune_snapshots(root)
    return sha


def new_turn(session_id: str) -> None:
    """Reset the per-turn snapshot dedup (called once at turn start)."""
    if not session_id:
        return
    _turn_counts[session_id] = _turn_counts.get(session_id, 0) + 1
    for key in list(_dedup):
        if key[0] == session_id:
            _dedup.discard(key)


def ensure_checkpoint(
    session_id: str, *, reason: str = "before_write", project_root: Path | None = None
) -> bool:
    """Take at most one snapshot per session/root/turn; fail-open."""
    if not SHADOW_GIT["enabled"] or not session_id:
        return True
    if project_root is None:
        from agent.tools.pub_base.path_utils import session_workspace_root

        root = session_workspace_root(session_id)
        if root is None:
            return True
        project_root = Path(root)
    root = project_root.resolve()
    key = (session_id, str(root))
    if key in _dedup:
        return True
    try:
        take_snapshot(root, reason, session_id=session_id)
        _dedup.add(key)
        return True
    except Exception as exc:  # noqa: BLE001 - snapshots must never block a write
        logger.warning("shadow git: checkpoint failed for {}: {}", root, exc)
        return False


def list_checkpoints(session_id: str, limit: int = 10, skip: int = 0) -> list[dict[str, Any]]:
    from agent.tools.pub_base.path_utils import session_workspace_root

    root = session_workspace_root(session_id)
    if root is None:
        return []
    root = Path(root)
    result = run_shadow(
        root,
        [
            "log",
            SHADOW_REF,
            "--date-order",
            f"-n{max(1, min(int(limit), 50))}",
            f"--skip={max(0, int(skip))}",
            "--pretty=format:%H%x1f%h%x1f%aI%x1f%s",
        ],
        timeout=10,
    )
    if result.returncode != 0:
        return []
    checkpoints: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if "\x1f" not in line:
            continue
        full, short, timestamp, message = line.split("\x1f", 3)
        fields = _parse_message(message)
        checkpoints.append(
            {
                "hash": short,
                "full_hash": full,
                "timestamp": timestamp,
                "turn": fields.get("turn", "?"),
                "type": fields.get("type", "unknown"),
                "tool": fields.get("tool", ""),
                "target": fields.get("target", ""),
                "summary": _summary(fields),
            }
        )
    return checkpoints


def list_restorable_files(
    session_id: str, commit: str, *, project_root: Path | None = None
) -> list[str]:
    from agent.tools.pub_base.path_utils import session_workspace_root

    root = project_root or session_workspace_root(session_id)
    if root is None:
        return []
    result = run_shadow(Path(root), ["ls-tree", "-r", "--name-only", commit], timeout=10)
    return [line for line in result.stdout.splitlines() if line.strip()]


def restore_checkpoint(
    session_id: str,
    commit: str,
    *,
    file_path: str | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    from agent.tools.pub_base.path_utils import session_workspace_root

    root = project_root or session_workspace_root(session_id)
    if root is None:
        raise RuntimeError("shadow git: session has no project directory")
    root = Path(root)
    target = commit.strip()
    if not target or target.startswith("-"):
        raise ValueError("invalid checkpoint hash")
    # Undo the undo: snapshot the current state before changing it.
    take_snapshot(root, f"pre_restore:{target}", session_id=session_id)
    path = file_path.strip() if file_path else "."
    if path.startswith("-"):
        raise ValueError("invalid file path")
    result = run_shadow(root, ["checkout", target, "--", path], timeout=30)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "git checkout failed").strip()[:300])
    return {"restored_to": target, "file": file_path or "all"}


def read_commit_files(
    session_id: str, commit: str, *, project_root: Path | None = None
) -> list[dict[str, str]]:
    from agent.tools.pub_base.path_utils import session_workspace_root

    root = project_root or session_workspace_root(session_id)
    if root is None:
        return []
    result = run_shadow(
        Path(root),
        ["show", "--no-patch", "--name-status", "--format=", commit],
        timeout=10,
    )
    files: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0][:1]
        if len(parts) >= 3 and status in {"R", "C"}:
            files.append({"status": status, "path": parts[2], "old_path": parts[1]})
        else:
            files.append({"status": status, "path": parts[1], "old_path": ""})
    return files


def read_commit_diff(
    session_id: str, commit: str, path: str, *, project_root: Path | None = None
) -> str:
    from agent.tools.pub_base.path_utils import session_workspace_root

    root = project_root or session_workspace_root(session_id)
    if root is None:
        return ""
    result = run_shadow(
        Path(root), ["show", "--format=", "--patch", commit, "--", path], timeout=10
    )
    return result.stdout
