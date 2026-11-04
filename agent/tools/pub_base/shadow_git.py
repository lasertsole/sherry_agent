"""Per-project shadow git snapshots.

Every sherry write takes a snapshot in ``<project>/.sherry/shadow-git`` using
``GIT_DIR``/``GIT_WORK_TREE``/``GIT_INDEX_FILE`` isolation. The user's ``.git``
is never initialized, staged, committed, reset, checked out or used for a
worktree. Snapshots are fail-open: a broken shadow store never blocks a write.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator
from typing import Any

from loguru import logger

from config.features import SHADOW_GIT, SUBAGENT_ISOLATION
from pub.git_head import SHADOW_REF, shadow_head

__all__ = [
    "SHADOW_REF",
    "diff_between_commits",
    "diff_between_commits_with_status",
    "ensure_checkpoint",
    "floor_for",
    "list_checkpoints",
    "list_restorable_files",
    "new_turn",
    "read_commit_diff",
    "read_commit_files",
    "restore_checkpoint",
    "restore_to_turn",
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

#: The process that owns the persisted floors. Turn numbers are
#: process-relative counters, so a floor written by a PREVIOUS process must
#: never be mistaken for this process's current turn (a collision is possible:
#: both processes start at turn 1). The boot id is the discriminator.
_BOOT_ID = uuid.uuid4().hex

#: (root key, session) -> {"turn": int, "tip": str}. ``tip`` is the chain tip
#: that preceded the session's current turn — the rollback boundary for editing
#: that turn's message. Mirrored into ``<store>/state.json`` (fail-open) so a
#: concurrent reader/pruner sees the same floors; entries from another boot are
#: discarded on load.
_floors: dict[tuple[str, str], dict[str, Any]] = {}
_floors_loaded: set[str] = set()
_floors_lock = threading.RLock()

#: Bound for the post-restore deletion pass (a runaway diff must not stall the
#: edit turn on thousands of unlinks).
_MAX_RESTORE_DELETIONS = 500


#: Per-root writer accounting for the eval teardown: wait_for_idle() must see
#: every in-flight snapshot/restore, and freeze() must stop late ones.
_writers_lock = threading.Lock()
_active_writers: dict[str, int] = {}
_frozen: set[str] = set()


def _root_key(project_root: Path) -> str:
    return str(project_root.resolve())


def shadow_store(project_root: Path) -> Path:
    """The shadow-git store for one project.

    ``SHADOW_GIT["store_dir"]`` (or ``SHERRY_SHADOW_GIT_DIR``) moves the store
    outside the project — the eval harness uses it so ``git clean`` never sees
    agent-private state. The default keeps the historical in-project path.
    """
    root = project_root.resolve()
    override = str(SHADOW_GIT.get("store_dir") or os.getenv("SHERRY_SHADOW_GIT_DIR", "")).strip()
    if override:
        digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
        return Path(override).expanduser().resolve() / digest
    return root / ".sherry" / "shadow-git"


def is_frozen(project_root: Path) -> bool:
    """True while the eval teardown has frozen snapshots for this root."""
    with _writers_lock:
        return _root_key(project_root) in _frozen


def freeze(project_root: Path) -> None:
    """Stop new snapshots/restores for this root (idempotent)."""
    with _writers_lock:
        _frozen.add(_root_key(project_root))


def unfreeze(project_root: Path) -> None:
    """Re-allow snapshots/restores for this root (idempotent)."""
    with _writers_lock:
        _frozen.discard(_root_key(project_root))


def active_writers(project_root: Path) -> int:
    """Number of in-flight snapshot/restore operations for this root."""
    with _writers_lock:
        return _active_writers.get(_root_key(project_root), 0)


def wait_for_idle(project_root: Path, *, timeout: float = 5.0) -> bool:
    """Wait until no snapshot/restore is in flight; False on timeout."""
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if active_writers(project_root) == 0:
            return True
        time.sleep(0.01)
    return active_writers(project_root) == 0


@contextmanager
def _writer(project_root: Path) -> Iterator[bool]:
    """Count one writer; yield False when the root is frozen."""
    key = _root_key(project_root)
    with _writers_lock:
        if key in _frozen:
            yield False
            return
        _active_writers[key] = _active_writers.get(key, 0) + 1
    try:
        yield True
    finally:
        with _writers_lock:
            remaining = _active_writers.get(key, 1) - 1
            if remaining > 0:
                _active_writers[key] = remaining
            else:
                _active_writers.pop(key, None)


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


def _state_path(project_root: Path) -> Path:
    return shadow_store(project_root) / "state.json"


def _load_floors(project_root: Path) -> None:
    """Load this root's persisted floors once (another boot's are stale)."""
    key = _root_key(project_root)
    with _floors_lock:
        if key in _floors_loaded:
            return
        _floors_loaded.add(key)
        try:
            data = json.loads(_state_path(project_root).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("boot") != _BOOT_ID:
            return
        raw = data.get("floors")
        if not isinstance(raw, dict):
            return
        for session_id, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            try:
                turn = int(entry.get("turn", -1))
            except (TypeError, ValueError):
                continue
            _floors[(key, str(session_id))] = {
                "turn": turn,
                "tip": str(entry.get("tip") or ""),
            }


def _save_floors(project_root: Path) -> None:
    """Persist this root's floors atomically; a write failure is fail-open."""
    key = _root_key(project_root)
    with _floors_lock:
        floors = {sid: dict(entry) for (rk, sid), entry in _floors.items() if rk == key}
    payload = {
        "boot": _BOOT_ID,
        "floors": {
            sid: {"turn": entry.get("turn", -1), "tip": entry.get("tip", "")}
            for sid, entry in floors.items()
            if entry.get("tip")
        },
    }
    try:
        path = _state_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.debug("shadow git: floor state write failed (fail-open): {}", exc)


def _record_floor(project_root: Path, session_id: str, turn: int, tip: str) -> None:
    """Remember the chain tip that preceded ``turn`` (first snapshot only)."""
    if not session_id or not tip:
        return
    _load_floors(project_root)
    key = (_root_key(project_root), session_id)
    with _floors_lock:
        entry = _floors.get(key)
        if entry is not None and int(entry.get("turn", -1)) == turn and entry.get("tip"):
            return
        _floors[key] = {"turn": turn, "tip": tip}
    _save_floors(project_root)


def _active_floor_tips(project_root: Path) -> list[str]:
    """Tips of floors that still belong to their session's CURRENT turn."""
    _load_floors(project_root)
    key = _root_key(project_root)
    with _floors_lock:
        entries = [(sid, dict(entry)) for (rk, sid), entry in _floors.items() if rk == key]
    tips: list[str] = []
    for session_id, entry in entries:
        tip = str(entry.get("tip") or "").strip()
        if not tip:
            continue
        if int(entry.get("turn", -1)) != _current_turn(session_id):
            continue
        tips.append(tip)
    return tips


def floor_for(session_id: str, *, project_root: Path | None = None) -> str | None:
    """The chain tip that preceded the session's CURRENT turn, if known.

    Written by the first snapshot of a turn (its parent). It is the rollback
    boundary for editing that turn's message, and :func:`_prune_snapshots`
    never prunes past it. ``None`` when the turn produced no snapshot (nothing
    to roll back), when the session has no project root, or after a restart
    (floors are process-scoped: a previous process's turn numbers cannot be
    trusted, and rolling back to an older turn would be destructive).
    """
    from agent.tools.pub_base.path_utils import session_workspace_root

    root = project_root or session_workspace_root(session_id)
    if root is None:
        return None
    root = Path(root).resolve()
    _load_floors(root)
    with _floors_lock:
        entry = _floors.get((_root_key(root), session_id))
        entry = dict(entry) if entry else None
    if not entry:
        return None
    if int(entry.get("turn", -1)) != _current_turn(session_id):
        return None
    tip = str(entry.get("tip") or "").strip()
    return tip or None


def _format_message(reason: str, session_id: str) -> str:
    parts = reason.split(":", 2)
    cp_type = parts[0] if parts else "unknown"
    tool = ""
    target = ""
    if cp_type == "before_write" and len(parts) == 3:
        tool, target = parts[1], parts[2]
    elif cp_type in {"pre_restore", "post_restore", "manual"}:
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
    if cp_type == "post_restore":
        return "post-restore (re-run base)"
    if cp_type == "manual":
        return f"manual: {target}".strip()
    return str(fields.get("raw", "checkpoint"))


def _prune_snapshots(project_root: Path) -> None:
    """Keep the NEWEST ``max_snapshots`` commits, never past a turn floor.

    The cap is a soft cap on HISTORY. A turn in progress may own more than
    ``max_snapshots`` commits (a fan-out child adds one per child, and the
    pre/post-restore pair adds two per edit), so the retained window is
    ``max(max_snapshots, everything since the OLDEST active floor)``. Once the
    turn ends its floor stops matching (``turn`` moved on) and the excess is
    released by the next snapshot's prune.

    A ref can only KEEP a commit and its ancestors, so dropping the OLD end
    means re-rooting its boundary. ``git replace --graft`` does that without
    rewriting: the boundary (and every newer commit) keeps its hash — the tip
    stays valid for isolation baselines, code-intel and explicit restore
    hashes — while ``rev-list``/``log`` stop at it. One tiny commit object per
    prune, no worktree scan.
    """
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
        ["rev-list", f"--skip={keep - 1}", "-n1", SHADOW_REF],
        timeout=10,
    )
    boundary = oldest.stdout.strip()
    if not boundary:
        return
    # A floor OLDER than the default boundary means the current turn owns more
    # than ``keep`` commits: keep everything from the floor on.
    boundary = _older_of(project_root, boundary, *_active_floor_tips(project_root))
    if not boundary:
        return
    parent = run_shadow(project_root, ["rev-parse", "--verify", f"{boundary}^"], timeout=10)
    if parent.returncode != 0:
        return  # already a root: its history is gone already
    graft = run_shadow(project_root, ["replace", "--graft", boundary], timeout=15)
    if graft.returncode != 0:
        logger.debug(
            "shadow git: prune graft failed (fail-open): {}",
            (graft.stderr or graft.stdout or "").strip()[:200],
        )


def _older_of(project_root: Path, *commits: str) -> str:
    """The commit with the MOST commits after it on ``SHADOW_REF`` (or "")."""
    candidates = [c.strip() for c in commits if c and c.strip()]
    if not candidates:
        return ""
    if len(candidates) == 1:
        # The default boundary comes from rev-list itself: it is on the chain,
        # so no ancestry check / distance measurement is needed.
        return candidates[0]
    best = ""
    best_distance = 0
    for candidate in candidates:
        ancestor = run_shadow(
            project_root, ["merge-base", "--is-ancestor", candidate, SHADOW_REF], timeout=10
        )
        if ancestor.returncode != 0:
            continue
        distance = run_shadow(
            project_root, ["rev-list", "--count", f"{candidate}..{SHADOW_REF}"], timeout=10
        )
        try:
            behind = int(distance.stdout.strip())
        except ValueError:
            continue
        if behind > best_distance:
            best, best_distance = candidate, behind
    return best


def take_snapshot(project_root: Path, reason: str, *, session_id: str = "") -> str | None:
    """Take one snapshot and return its commit hash (None on failure/frozen)."""
    root = project_root.resolve()
    with _writer(root) as allowed:
        if not allowed:
            return None
        return _take_snapshot_locked(root, reason, session_id=session_id)


def _take_snapshot_locked(root: Path, reason: str, *, session_id: str = "") -> str | None:
    _ensure_store(root)
    add = run_shadow(root, ["add", "-A"])
    if add.returncode != 0:
        raise RuntimeError((add.stderr or add.stdout or "git add failed").strip()[:300])
    tree = run_shadow(root, ["write-tree"], timeout=10).stdout.strip()
    if not tree:
        raise RuntimeError("git write-tree returned no tree")
    parent = run_shadow(root, ["rev-parse", "--verify", SHADOW_REF], timeout=10)
    parent_args = ["-p", parent.stdout.strip()] if parent.returncode == 0 else []
    if parent.returncode == 0:
        # The first snapshot of a turn remembers the tip that preceded it: the
        # boundary an edit of that turn rolls back to (and the prune floor).
        _record_floor(root, session_id, _current_turn(session_id), parent.stdout.strip())
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


def diff_changed_paths(project_root: Path, *, cap: int = 200) -> list[str]:
    """Project-relative paths whose working tree differs from the last snapshot.

    The shell accounting seam (design: shell writes bypass the file tools, so
    they used to leave no ledger row at all). ``ensure_checkpoint`` has already
    stored the pre-command state, so one ``git diff --name-only`` names exactly
    what the command changed — content comparison, so a ``touch -d`` cannot
    fake it. Fail-open: a missing store, a broken repo or a timeout yields ``[]``.
    """
    root = project_root.resolve()
    try:
        result = run_shadow(root, ["diff", "--name-only", SHADOW_REF], timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("shadow diff failed (fail-open): {}", exc)
        return []
    if result.returncode != 0:
        return []
    paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return paths[:cap]


def _run_shadow_readonly(
    project_root: Path, args: list[str], *, timeout: float = 10
) -> subprocess.CompletedProcess[str] | None:
    """Run a read-only git command against an EXISTING shadow store.

    Unlike :func:`run_shadow`, this never calls ``_ensure_store``: a read path
    must not create ``.sherry/shadow-git`` or rewrite its ``info/exclude`` just
    to ask a question. ``None`` means the store is absent (or unreadable), and
    the caller must fall back to its non-shadow behaviour.
    """
    root = project_root.resolve()
    store = shadow_store(root)
    if not (store / "HEAD").exists():
        return None
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(root),
            env={**os.environ, **shadow_env(root)},
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("shadow read-only git failed (fail-open): {}", exc)
        return None


def diff_between_commits(
    project_root: Path, from_sha: str, to_sha: str, *, cap: int = 500
) -> list[str]:
    """Project-relative paths that differ between two shadow snapshots.

    READ-ONLY: a missing store or any git failure yields ``[]``; the store is
    never created or mutated. Deleted paths ARE included (git's own semantics);
    callers that need the status use :func:`diff_between_commits_with_status`.
    """
    result = _run_shadow_readonly(project_root, ["diff", "--name-only", f"{from_sha}..{to_sha}"])
    if result is None or result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()][:cap]


def diff_between_commits_with_status(
    project_root: Path, from_sha: str, to_sha: str, *, cap: int = 500
) -> list[tuple[str, str, str | None]]:
    """``git diff --name-status`` between two snapshots, as structured entries.

    Returns ``[(status, path, renamed_to), ...]`` where ``status`` is the first
    letter of git's status code (``A``/``M``/``D``/``R``/``C``); ``renamed_to``
    is the new path for ``R``/``C`` and ``None`` otherwise. READ-ONLY and
    fail-open: a missing store, a bad revision or a timeout yields ``[]``.
    """
    result = _run_shadow_readonly(project_root, ["diff", "--name-status", f"{from_sha}..{to_sha}"])
    if result is None or result.returncode != 0:
        return []
    entries: list[tuple[str, str, str | None]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0]:
            continue
        status = parts[0][0]
        if status in {"R", "C"} and len(parts) >= 3:
            entries.append((status, parts[1], parts[2]))
        else:
            entries.append((status, parts[1], None))
        if len(entries) >= cap:
            break
    return entries


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
    if is_frozen(root):
        return True
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


def _delete_new_files_since(root: Path, target: str) -> list[str]:
    """Delete worktree files that did not exist in ``target``.

    ``git checkout <commit> -- .`` restores and overwrites, but never deletes:
    a file CREATED during the turn would survive the rollback. The difference
    between the shadow index (last snapshot) and the target tree, plus the
    untracked set, is exactly what appeared after the boundary. Git's own
    exclude rules do the filtering, so ``.git``/``.sherry``/secrets are never
    candidates. Fail-open and bounded.
    """
    tree = run_shadow(root, ["ls-tree", "-r", "--name-only", target], timeout=10)
    if tree.returncode != 0:
        return []
    keep = {line.strip() for line in tree.stdout.splitlines() if line.strip()}
    index = run_shadow(root, ["ls-files"], timeout=10)
    if index.returncode != 0:
        return []
    indexed = {line.strip() for line in index.stdout.splitlines() if line.strip()}
    others = run_shadow(root, ["ls-files", "--others", "--exclude-standard"], timeout=10)
    untracked = (
        {line.strip() for line in others.stdout.splitlines() if line.strip()}
        if others.returncode == 0
        else set()
    )
    doomed = sorted((indexed - keep) | untracked)[:_MAX_RESTORE_DELETIONS]
    removed: list[str] = []
    for rel in doomed:
        candidate = root / rel
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(root):
            continue
        try:
            if resolved.is_file() or resolved.is_symlink():
                resolved.unlink()
                removed.append(rel)
        except OSError as exc:
            logger.debug("shadow git: cannot remove {} (fail-open): {}", rel, exc)
    if removed:
        run_shadow(root, ["update-index", "--force-remove", "--", *removed], timeout=15)
        for rel in removed:
            parent = (root / rel).parent
            while parent != root and parent.is_dir():
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
    return removed


def restore_to_turn(
    session_id: str,
    before_turn: int,
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Restore the workspace to the start of the session's CURRENT turn.

    This is the file half of an edit-message rewind: the caller has already
    truncated the transcript (that step is the one that can still refuse, so it
    runs first) and :func:`floor_for` names the chain tip that preceded the
    turn.

    Returns ``{"restored": True, "commit": ..., "removed": [...]}`` or
    ``{"restored": False, "reason": ...}`` with ``no_root`` / ``no_checkpoint``
    (nothing was written this turn, or the floor belongs to a previous
    process) / ``frozen`` / ``failed``. NEVER raises: the caller proceeds with
    the transcript edit either way (fail-open).
    """
    try:
        return _restore_to_turn_impl(session_id, before_turn, project_root=project_root)
    except Exception as exc:  # noqa: BLE001 - a rewind must not fail the edit
        logger.warning("shadow git: restore_to_turn failed: {}", exc)
        return {"restored": False, "reason": "failed"}


def _restore_to_turn_impl(
    session_id: str, before_turn: int, *, project_root: Path | None
) -> dict[str, Any]:
    from agent.tools.pub_base.path_utils import session_workspace_root

    root = project_root or session_workspace_root(session_id)
    if root is None:
        return {"restored": False, "reason": "no_root"}
    root = Path(root).resolve()
    target = floor_for(session_id, project_root=root)
    if target is None:
        return {"restored": False, "reason": "no_checkpoint"}
    if is_frozen(root):
        return {"restored": False, "reason": "frozen"}
    with _writer(root) as allowed:
        if not allowed:
            return {"restored": False, "reason": "frozen"}
        return _restore_to_commit_locked(root, session_id, target, before_turn)


def _restore_to_commit_locked(
    root: Path, session_id: str, target: str, before_turn: int
) -> dict[str, Any]:
    verify = run_shadow(root, ["cat-file", "-e", f"{target}^{{commit}}"], timeout=10)
    if verify.returncode != 0:
        return {"restored": False, "reason": "no_checkpoint"}
    # Undo point: the state being replaced becomes a chain commit.
    try:
        _take_snapshot_locked(root, f"pre_restore:edit_turn_{before_turn}", session_id=session_id)
    except Exception as exc:  # noqa: BLE001 - the restore is still worth trying
        logger.debug("shadow git: pre-restore snapshot failed (fail-open): {}", exc)
    checkout = run_shadow(root, ["checkout", target, "--", "."], timeout=30)
    if checkout.returncode != 0:
        return {
            "restored": False,
            "reason": "failed",
            "detail": (checkout.stderr or checkout.stdout or "").strip()[:300],
        }
    removed = _delete_new_files_since(root, target)
    # The chain tip must describe the RESTORED tree: the re-run turn's floor is
    # this snapshot, so editing that turn later must not resurrect the files
    # this rollback just removed.
    try:
        _take_snapshot_locked(root, f"post_restore:edit_turn_{before_turn}", session_id=session_id)
    except Exception as exc:  # noqa: BLE001 - the rollback itself already landed
        logger.debug("shadow git: post-restore snapshot failed (fail-open): {}", exc)
    return {"restored": True, "commit": target, "removed": removed}


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
