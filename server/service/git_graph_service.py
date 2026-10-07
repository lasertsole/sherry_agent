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

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from config.features import GIT_GRAPH
from runtime.session.project_dir import current_project_dir, project_dir_source

__all__ = ["GraphPage", "read_graph"]

#: Field separator inside one commit record (ASCII unit separator) and the
#: record separator between commits — byte values git never puts in a subject.
_FIELD = "\x1f"
_RECORD = "\x1e"

_PRETTY = f"%H{_FIELD}%P{_FIELD}%an{_FIELD}%aI{_FIELD}%D{_FIELD}%s{_RECORD}"


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
    )
