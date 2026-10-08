"""Read-only git history for the left sidebar's graph panel.

One bounded ``git log --all --date-order`` per call, run in the SESSION's project
directory (the same root the file browser and every tool resolve against), plus
the current branch and a dirty-file count for the header. Nothing here writes to
a repository, and a directory that is not a repository is a normal answer
(``available: false`` with a reason), never an exception:

- a missing ``git`` binary, a non-repository directory and a repository with no
  commits all surface as an empty graph the UI renders as its own empty state;
- text is bounded by :data:`GIT_GRAPH` (a subject is one UI row) and every git
  call carries a timeout, so a huge or wedged repository cannot hold the request;
- parents / refs / subjects are parsed from an ASCII record separator so a
  multi-line commit message cannot break the framing.

The blocking work runs in a worker thread (``asyncio.to_thread`` at the route
layer), matching the file-browser service.
"""

from __future__ import annotations

import difflib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from config.features import GIT_GRAPH
from runtime.session.project_dir import current_project_dir, project_dir_source

__all__ = [
    "CommitDetail",
    "CommitDiff",
    "GitActionError",
    "GraphPage",
    "read_commit_diff",
    "read_commit_files",
    "read_graph",
    "reset_to",
    "checkout_ref",
]

#: Field separator inside one commit record (ASCII unit separator) and the
#: record separator between commits — byte values git never puts in a subject.
_FIELD = "\x1f"
_RECORD = "\x1e"

_PRETTY = f"%H{_FIELD}%P{_FIELD}%an{_FIELD}%aI{_FIELD}%D{_FIELD}%s{_RECORD}"


class GitActionError(Exception):
    """A refused / failed write with a client-safe message and an HTTP status."""

    def __init__(self, reason: str, status: int = 400) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class GraphPage:
    """One page of history plus the header facts the panel shows."""

    root: str
    source: str
    available: bool
    reason: str = ""
    branch: str = ""
    detached: bool = False
    dirty: int = 0
    dirty_capped: bool = False
    commits: list[dict] = field(default_factory=list)
    has_more: bool = False
    #: Local branches, most recently committed first — the branch switcher's list.
    branches: list[str] = field(default_factory=list)


def _run(root: Path, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run ``git <args>`` bounded; ``None`` when git is missing or timed out."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=GIT_GRAPH["timeout_s"],
        )
    except FileNotFoundError:
        logger.info("git graph: git binary not available")
        return None
    except subprocess.TimeoutExpired:
        logger.warning(
            "git graph: '{}' timed out after {}s", " ".join(args[:2]), GIT_GRAPH["timeout_s"]
        )
        return None


def _text(value: str) -> str:
    """Bound one served string (:data:`GIT_GRAPH`'s row width)."""
    return value.strip()[: GIT_GRAPH["max_text_chars"]]


def _refs_of(raw: str) -> list[dict[str, str]]:
    """Parse ``%D`` into chips: ``[{kind, name}]`` (head / branch / tag / remote)."""
    chips: list[dict[str, str]] = []
    for part in (piece.strip() for piece in raw.split(",")):
        if not part:
            continue
        if part.startswith("HEAD -> "):
            chips.append({"kind": "head", "name": _text(part[len("HEAD -> ") :])})
        elif part == "HEAD":
            chips.append({"kind": "head", "name": "HEAD"})
        elif part.startswith("tag: "):
            chips.append({"kind": "tag", "name": _text(part[len("tag: ") :])})
        elif part.startswith("origin/") or "/" in part:
            chips.append({"kind": "remote", "name": _text(part)})
        else:
            chips.append({"kind": "branch", "name": _text(part)})
    return chips


def _parse_log(stdout: str) -> list[dict]:
    """Parse the ``%x1f``/``%x1e`` framed log into commit dicts."""
    commits: list[dict] = []
    for record in stdout.split(_RECORD):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split(_FIELD)
        if len(parts) < 6:
            continue
        full, parents, author, date, refs, subject = parts[:6]
        commits.append(
            {
                "hash": full,
                "short": full[:8],
                "parents": [parent for parent in parents.split() if parent],
                "author": _text(author),
                "date": _text(date),
                "refs": _refs_of(refs),
                "subject": _text(subject),
            }
        )
    return commits


def _header(root: Path) -> tuple[bool, str, str, bool, int, bool]:
    """``(available, reason, branch, detached, dirty, dirty_capped)``."""
    probe = _run(root, ["rev-parse", "--is-inside-work-tree"])
    if probe is None:
        return False, "git-unavailable", "", False, 0, False
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return False, "not-a-repository", "", False, 0, False

    branch = ""
    detached = False
    head = _run(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if head is not None and head.returncode == 0:
        name = head.stdout.strip()
        # A repository without commits answers "HEAD" here as well; the log then
        # comes back empty and the panel says so.
        detached = name == "HEAD"
        branch = _text(name)

    dirty = 0
    capped = False
    status = _run(root, ["status", "--porcelain"])
    if status is not None and status.returncode == 0:
        lines = [line for line in status.stdout.splitlines() if line.strip()]
        capped = len(lines) > GIT_GRAPH["max_status_entries"]
        dirty = min(len(lines), GIT_GRAPH["max_status_entries"])
    return True, "", branch, detached, dirty, capped


def _local_branches(root: Path) -> list[str]:
    """Local branch names, most recently committed first (the switcher's list).

    ``--sort=-committerdate`` puts the branches the user is likely to want at the
    top (the panel marks the current one), and the list is clipped so a
    repository with thousands of branches cannot bloat the page.
    """
    refs = _run(
        root,
        [
            "for-each-ref",
            "--sort=-committerdate",
            f"--count={GIT_GRAPH['max_branches']}",
            "--format=%(refname:short)",
            "refs/heads",
        ],
    )
    if refs is None or refs.returncode != 0:
        return []
    return [_text(line) for line in refs.stdout.splitlines() if line.strip()][
        : GIT_GRAPH["max_branches"]
    ]


def read_graph(session_id: str, limit: int | None = None, skip: int = 0) -> GraphPage:
    """One page of the session project's history (newest first).

    :param session_id: Session whose project directory is read.
    :param limit: Commits per page (clamped to ``1..max_page_size``).
    :param skip: Commits to skip (the panel's paging).
    :returns: The page, with ``available: False`` + a reason for a directory that
        is not a usable repository (the UI renders its own empty state).
    """
    root = current_project_dir(session_id)
    page_size = limit if isinstance(limit, int) and limit > 0 else GIT_GRAPH["page_size"]
    page_size = min(page_size, GIT_GRAPH["max_page_size"])
    offset = max(skip if isinstance(skip, int) else 0, 0)

    available, reason, branch, detached, dirty, capped = _header(root)
    if not available:
        return GraphPage(
            root=str(root),
            source=project_dir_source(session_id),
            available=False,
            reason=reason,
        )

    # One extra commit tells us whether another page exists (never served).
    log = _run(
        root,
        [
            "log",
            "--all",
            "--date-order",
            f"-n{page_size + 1}",
            f"--skip={offset}",
            f"--pretty=format:{_PRETTY}",
        ],
    )
    commits = _parse_log(log.stdout) if log is not None and log.returncode == 0 else []
    has_more = len(commits) > page_size
    return GraphPage(
        root=str(root),
        source=project_dir_source(session_id),
        available=True,
        branch=branch,
        detached=detached,
        dirty=dirty,
        dirty_capped=capped,
        commits=commits[:page_size],
        has_more=has_more,
        branches=_local_branches(root),
    )


def _stderr_tail(completed: subprocess.CompletedProcess[str] | None) -> str:
    """A bounded, single-line git error message for the client."""
    if completed is None:
        return "git did not answer in time"
    text = (completed.stderr or completed.stdout or "").strip().replace("\n", " ")
    return text[: GIT_GRAPH["max_text_chars"]] or "git refused the operation"


def _require_repository(root: Path) -> None:
    """Refuse a write outside a usable repository (the read paths say so in JSON)."""
    probe = _run(root, ["rev-parse", "--is-inside-work-tree"])
    if probe is None:
        raise GitActionError("git is not available on the server", 503)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        raise GitActionError("the working directory is not a git repository", 400)


#: ``git reset`` modes the panel may ask for (``--hard`` discards the working
#: tree changes; the UI warns, the backend only validates the mode).
_RESET_MODES: frozenset[str] = frozenset({"soft", "mixed", "hard"})


def reset_to(session_id: str, target: str, mode: str) -> GraphPage:
    """Move the current branch to *target* (``git reset --<mode> <target>``).

    The panel's 回退 action. The target must resolve to a COMMIT in this
    repository, and ``mode`` must be one of soft / mixed / hard — anything else
    is refused before git runs, so a crafted request cannot smuggle flags.
    Returns the refreshed page so the caller repaints in one round trip.
    """
    if mode not in _RESET_MODES:
        raise GitActionError(f"unknown reset mode: {mode}")
    clean_target = target.strip()
    if not clean_target or clean_target.startswith("-"):
        raise GitActionError("invalid commit")
    root = current_project_dir(session_id)
    _require_repository(root)
    verify = _run(root, ["rev-parse", "--verify", f"{clean_target}^{{commit}}"])
    if verify is None:
        raise GitActionError("git is not available on the server", 503)
    if verify.returncode != 0:
        raise GitActionError(f"unknown commit: {clean_target}", 404)
    result = _run(root, ["reset", f"--{mode}", clean_target])
    if result is None or result.returncode != 0:
        raise GitActionError(_stderr_tail(result), 409)
    logger.info("git graph: reset --{} to {} in {}", mode, clean_target[:8], root)
    return read_graph(session_id)


def checkout_ref(session_id: str, ref: str) -> GraphPage:
    """Check out a branch (or any resolvable ref): the panel's 切换分支 action.

    A conflicting checkout fails inside git and its stderr comes back as-is; a
    ref git does not know is refused before anything is touched.
    """
    clean_ref = ref.strip()
    if not clean_ref or clean_ref.startswith("-"):
        raise GitActionError("invalid ref")
    root = current_project_dir(session_id)
    _require_repository(root)
    verify = _run(root, ["rev-parse", "--verify", f"{clean_ref}^{{commit}}"])
    if verify is None:
        raise GitActionError("git is not available on the server", 503)
    if verify.returncode != 0:
        raise GitActionError(f"unknown ref: {clean_ref}", 404)
    result = _run(root, ["checkout", clean_ref])
    if result is None or result.returncode != 0:
        raise GitActionError(_stderr_tail(result), 409)
    logger.info("git graph: checked out {} in {}", clean_ref, root)
    return read_graph(session_id)


@dataclass(frozen=True)
class CommitFile:
    """One file a commit touched."""

    status: str
    path: str
    old_path: str = ""


@dataclass(frozen=True)
class CommitDetail:
    """A commit's metadata plus the files it touched."""

    hash: str
    short: str
    author: str
    date: str
    parents: list[str]
    subject: str
    files: list[CommitFile]


@dataclass(frozen=True)
class DiffRow:
    """One aligned row of a side-by-side diff (a side may be absent)."""

    left: dict | None
    right: dict | None


@dataclass(frozen=True)
class CommitDiff:
    """One file's diff inside one commit, pre-aligned for the two-column view."""

    hash: str
    path: str
    old_path: str
    status: str
    old_label: str
    new_label: str
    rows: list[DiffRow]
    truncated: bool = False
    binary: bool = False
    notice: str = ""


def _parse_name_status(raw: str) -> list[CommitFile]:
    """Parse ``--name-status -z`` output: ``<status>NUL<path>NUL`` per entry.

    ``-z`` is what keeps a non-ASCII or space-bearing path intact — the default
    line format quotes such a path (``"\\345\\256\\236"``) and the quoting then
    breaks both the display and the entry lookup behind the diff view. Renames
    and copies carry two paths, old first.
    """
    tokens = raw.split("\0")
    files: list[CommitFile] = []
    index = 0
    while index < len(tokens):
        status = tokens[index].strip()
        index += 1
        if not status:
            continue
        if status[0] in ("R", "C"):
            old_path = tokens[index] if index < len(tokens) else ""
            new_path = tokens[index + 1] if index + 1 < len(tokens) else ""
            index += 2
            files.append(CommitFile(status=status[0], path=new_path, old_path=old_path))
            continue
        files.append(
            CommitFile(status=status[0], path=tokens[index] if index < len(tokens) else "")
        )
        index += 1
    return files


def _parents_of(root: Path, hash: str) -> list[str]:
    """The commit's parent hashes (empty for a root commit)."""
    show = _run(root, ["rev-list", "--parents", "-n1", hash])
    if show is None or show.returncode != 0:
        return []
    parts = show.stdout.split()
    return [part for part in parts[1:] if part]


def read_commit_files(session_id: str, hash: str) -> CommitDetail:
    """One commit's metadata + the files it touched (the graph row's expansion)."""
    clean = hash.strip()
    if not clean or clean.startswith("-"):
        raise GitActionError("invalid commit")
    root = current_project_dir(session_id)
    _require_repository(root)
    meta = _run(
        root,
        [
            "show",
            "--no-patch",
            f"--pretty=format:%H{_FIELD}%an{_FIELD}%aI{_FIELD}%s",
            clean,
        ],
    )
    if meta is None:
        raise GitActionError("git is not available on the server", 503)
    if meta.returncode != 0:
        raise GitActionError(f"unknown commit: {clean}", 404)
    fields = meta.stdout.split(_FIELD)
    names = _run(root, ["show", "--name-status", "--format=", "-M", "-z", clean])
    files: list[CommitFile] = []
    if names is not None and names.returncode == 0:
        files = _parse_name_status(names.stdout)
    full = fields[0] if fields else clean
    return CommitDetail(
        hash=full,
        short=full[:8],
        author=_text(fields[1]) if len(fields) > 1 else "",
        date=_text(fields[2]) if len(fields) > 2 else "",
        parents=_parents_of(root, clean),
        subject=_text(fields[3]) if len(fields) > 3 else "",
        files=files,
    )


def _blob(root: Path, revision: str) -> tuple[str | None, str]:
    """The text of ``revision`` (``<hash>:<path>``); ``(None, reason)`` when unusable."""
    probe = _run(root, ["cat-file", "-s", revision])
    if probe is None:
        return None, "git-unavailable"
    if probe.returncode != 0:
        return None, "missing"
    try:
        size = int(probe.stdout.strip() or "0")
    except ValueError:
        return None, "missing"
    if size > GIT_GRAPH["max_diff_bytes"]:
        return None, "too-large"
    blob = subprocess.run(
        ["git", "show", revision],
        cwd=str(root),
        capture_output=True,
        timeout=GIT_GRAPH["timeout_s"],
    )
    if blob.returncode != 0:
        return None, "missing"
    if b"\x00" in blob.stdout[:8192]:
        return None, "binary"
    try:
        return blob.stdout.decode("utf-8"), ""
    except UnicodeDecodeError:
        return None, "binary"


def _aligned_rows(old_text: str, new_text: str) -> tuple[list[DiffRow], bool]:
    """Align two texts into side-by-side rows (the stdlib's diff opcodes)."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    rows: list[DiffRow] = []
    truncated = False
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                if len(rows) >= GIT_GRAPH["max_diff_rows"]:
                    return rows, True
                rows.append(
                    DiffRow(
                        left={"n": i1 + offset + 1, "text": old_lines[i1 + offset], "kind": "same"},
                        right={
                            "n": j1 + offset + 1,
                            "text": new_lines[j1 + offset],
                            "kind": "same",
                        },
                    )
                )
            continue
        # replace / delete / insert: pair the two sides line by line so the
        # changed block reads as "old | new" (VS Code's alignment).
        removed = old_lines[i1:i2]
        added = new_lines[j1:j2]
        for offset in range(max(len(removed), len(added))):
            if len(rows) >= GIT_GRAPH["max_diff_rows"]:
                truncated = True
                break
            left = (
                {"n": i1 + offset + 1, "text": removed[offset], "kind": "remove"}
                if offset < len(removed)
                else None
            )
            right = (
                {"n": j1 + offset + 1, "text": added[offset], "kind": "add"}
                if offset < len(added)
                else None
            )
            rows.append(DiffRow(left=left, right=right))
        if truncated:
            break
    return rows, truncated


def read_commit_diff(session_id: str, hash: str, path: str) -> CommitDiff:
    """One file's diff inside one commit, aligned for the two-column view.

    The sides come from the commit's own blobs (``<parent>:<path>`` and
    ``<hash>:<path>``) rather than a textual patch, which is what makes an added
    or deleted file render as a SINGLE column and a rename follow its old path.
    """
    clean = hash.strip()
    target = path.strip()
    if not clean or clean.startswith("-") or not target:
        raise GitActionError("invalid commit or path")
    root = current_project_dir(session_id)
    _require_repository(root)
    detail = read_commit_files(session_id, clean)
    entry = next(
        (item for item in detail.files if item.path == target or item.old_path == target), None
    )
    if entry is None:
        raise GitActionError(f"path is not part of the commit: {target}", 404)
    status = entry.status
    old_path = entry.old_path if entry.old_path else target
    parent = detail.parents[0] if detail.parents else ""

    old_text = ""
    new_text = ""
    notice = ""
    if status != "A" and parent:
        old_text, reason = _blob(root, f"{parent}:{old_path}")
        if old_text is None and reason == "binary":
            return CommitDiff(
                hash=detail.hash,
                path=target,
                old_path=old_path,
                status=status,
                old_label=f"{parent[:8]}:{old_path}",
                new_label=f"{detail.short}:{target}",
                rows=[],
                binary=True,
            )
        if old_text is None and reason == "too-large":
            notice = "too-large"
        old_text = old_text or ""
    if status != "D":
        new_text, reason = _blob(root, f"{detail.hash}:{target}")
        if new_text is None and reason == "binary":
            return CommitDiff(
                hash=detail.hash,
                path=target,
                old_path=old_path,
                status=status,
                old_label=f"{parent[:8]}:{old_path}" if parent else "",
                new_label=f"{detail.short}:{target}",
                rows=[],
                binary=True,
            )
        if new_text is None and reason == "too-large":
            notice = "too-large"
        new_text = new_text or ""

    rows, truncated = _aligned_rows(old_text, new_text)
    return CommitDiff(
        hash=detail.hash,
        path=target,
        old_path=old_path,
        status=status,
        old_label=f"{parent[:8]}:{old_path}" if parent and status != "A" else "",
        new_label=f"{detail.short}:{target}" if status != "D" else "",
        rows=rows,
        truncated=truncated,
        notice=notice,
    )
