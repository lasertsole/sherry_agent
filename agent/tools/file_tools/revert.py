"""``file_changes_revert`` — put files back the way they were, or refuse loudly.

The plan/apply split follows the snapshot rows: each path is restored to its
EARLIEST snapshotted content (the state before this turn's first write to it),
and every action is checked against what the disk holds right now before
anything is touched.

Two properties matter more than convenience:

* **A stale file refuses the WHOLE batch.** Restoring over an edit the revert
  did not make is the failure mode this feature exists to prevent (ZCode's
  silent overwrite), so a batch with one unsafe file changes nothing and
  reports every path with its reason.
* **A refusal still leaves the user a way forward.** When the target sits in a
  git work tree, the plan additionally probes whether a three-way merge would
  be clean (``git merge-file -p`` on temporary copies, never the work tree) and
  reports the command to run — information only; this tool never merges.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal, override

from loguru import logger
from pydantic import BaseModel, Field

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool, InjectedToolCallId
from langgraph.prebuilt.tool_node import InjectedState

from agent.tools.pub_base import (
    _extract_session_id,
    atomic_write_bytes_no_follow,
    file_revision,
    read_bytes_no_follow,
    safe_error_detail,
)
from runtime.session.project_dir import current_project_dir

from .snapshot import SnapshotRow, blob_bytes, delete_rows, rows_for_session

__all__ = [
    "FileChangesRevertInput",
    "RevertAction",
    "RevertPlan",
    "build_file_changes_revert_tool",
    "revert_file_changes",
]

SessionId = Annotated[str, InjectedState("session_id")]
ToolCallId = Annotated[str, InjectedToolCallId]

#: A path's verdict in the plan.
Action = Literal["restore", "delete", "none"]


class FileChangesRevertInput(BaseModel):
    """Arguments the model (or the HTTP endpoint) supplies."""

    session_id: SessionId = ""
    tool_call_id: ToolCallId = ""
    paths: list[str] = Field(
        default_factory=list,
        description="Files to revert (project-relative or absolute). Empty = every "
        "path this session has snapshots for.",
    )
    to_tool_call_id: str = Field(
        default="",
        description="Revert the changes made up to AND INCLUDING this tool call.",
    )
    dry_run: bool = Field(
        default=False,
        description="Return the plan without touching the disk.",
    )


@dataclass(slots=True)
class RevertAction:
    """One file's planned fate."""

    path: str
    action: Action
    row: SnapshotRow | None = None  # earliest in scope: the content to restore
    safe: bool = True
    reason: str = ""
    latest: SnapshotRow | None = None  # newest for the path: the freshness row

    def as_dict(self) -> dict:
        payload = {"path": self.path, "action": self.action, "safe": self.safe}
        if self.reason:
            payload["reason"] = self.reason
        return payload


@dataclass(slots=True)
class RevertPlan:
    """Everything a revert would do, with the reasons it would not."""

    actions: list[RevertAction] = field(default_factory=list)
    merge3way: dict | None = None

    @property
    def safe(self) -> bool:
        """True when every file may be applied (the batch rule)."""
        return all(action.safe for action in self.actions)

    @property
    def changes_anything(self) -> bool:
        return any(action.action != "none" for action in self.actions)

    def as_dict(self) -> dict:
        return {
            "safe": self.safe,
            "files": [action.as_dict() for action in self.actions],
            "merge3way": self.merge3way,
        }


def _normalize(session_id: str, raw: str, row: SnapshotRow | None) -> str:
    """Absolute path for a request entry, resolved against the row's own root.

    A snapshot row stores the root that was active when it was captured
    (project directories can be re-bound between turns), and that recorded root
    is what explains the path again — never today's binding.
    """
    path = Path(raw)
    if path.is_absolute():
        return str(path)
    root = Path(row.root) if row is not None and row.root else current_project_dir(session_id)
    return str((root / raw).resolve())


def _hash_guard_allows(size: int) -> bool:
    from config.features import FILE_SNAPSHOT

    return size <= int(FILE_SNAPSHOT["hash_guard_max_bytes"])


def _classify(session_id: str, row: SnapshotRow, latest: SnapshotRow) -> RevertAction:
    """Decide whether one path may be restored, and why not when it may not.

    :param row: The earliest in-scope row — where the restored content comes from.
    :param latest: The path's newest row — what the disk is compared against.
    """
    import hashlib

    target = Path(row.path)
    current_revision = file_revision(target)

    def _matches_expected() -> bool:
        """Does the disk still hold what this session last wrote?"""
        if current_revision == latest.after_revision:
            return True
        data = _read_bytes(target)
        if data is None or not _hash_guard_allows(len(data)):
            return False
        return hashlib.sha256(data).hexdigest() == latest.after_sha256

    if not row.existed_before:
        # The write CREATED the file: reverting means removing it.
        if current_revision == "absent":
            return RevertAction(
                row.path, "none", row, safe=True, reason="already absent", latest=latest
            )
        if _matches_expected():
            return RevertAction(row.path, "delete", row, latest=latest)
        return RevertAction(
            row.path, "delete", row, safe=False, reason="external_modified", latest=latest
        )

    # The write REPLACED an existing file: reverting means restoring the blob.
    if current_revision == "absent":
        return RevertAction(row.path, "restore", row, safe=False, reason="missing", latest=latest)
    if blob_bytes(session_id, row.blob_sha256 or "") is None:
        return RevertAction(
            row.path, "restore", row, safe=False, reason="snapshot_expired", latest=latest
        )
    if _matches_expected():
        return RevertAction(row.path, "restore", row, latest=latest)
    return RevertAction(
        row.path, "restore", row, safe=False, reason="external_modified", latest=latest
    )


def _read_bytes(path: Path) -> bytes | None:
    try:
        data, _stat = read_bytes_no_follow(path)
        return data
    except OSError:
        return None


def _select_rows(
    session_id: str,
    paths: list[str],
    to_tool_call_id: str,
) -> tuple[dict[str, SnapshotRow], dict[str, SnapshotRow]]:
    """Rows to restore from, and rows that describe the file as it stands now.

    Two different questions, two different rows:

    * the CONTENT to restore comes from the EARLIEST row inside the requested
      scope — "undo" means the state before the first write in scope;
    * the FRESHNESS check compares the disk against the LATEST row of that path
      anywhere in the session, because that is what this session last wrote.
      Checking against the earliest row would make a path written twice
      unrevertable (the disk holds the newest of the agent's own writes).

    :returns: ``(earliest_in_scope, latest_overall)`` keyed by path.
    """
    rows = rows_for_session(session_id)
    if not rows:
        return {}, {}
    upper = None
    if to_tool_call_id:
        matching = [row.captured_at for row in rows if row.tool_call_id == to_tool_call_id]
        upper = max(matching) if matching else None
        if upper is None:
            return {}

    def _matches(row: SnapshotRow) -> bool:
        """Does this row answer one of the requested paths?

        Each request entry is resolved against the ROOT THE ROW RECORDED — a
        project directory can be re-bound between turns, and today's binding
        must never be used to reinterpret yesterday's snapshot.
        """
        if not paths:
            return True
        for raw in paths:
            if Path(raw).is_absolute():
                if str(Path(raw)) == row.path:
                    return True
                continue
            if _normalize(session_id, raw, row) == row.path:
                return True
        return False

    latest_overall: dict[str, SnapshotRow] = {}
    for row in rows:
        if not _matches(row):
            continue
        current = latest_overall.get(row.path)
        if current is None or (row.captured_at, row.id) > (current.captured_at, current.id):
            latest_overall[row.path] = row

    earliest_in_scope: dict[str, SnapshotRow] = {}
    for row in rows:
        if upper is not None and row.captured_at > upper:
            continue
        if not _matches(row):
            continue
        current = earliest_in_scope.get(row.path)
        if current is None or (row.captured_at, row.id) < (current.captured_at, current.id):
            earliest_in_scope[row.path] = row
    return earliest_in_scope, latest_overall


def _probe_merge3way(actions: list[RevertAction]) -> dict | None:
    """Read-only three-way-merge feasibility for a refused batch.

    ``git merge-file -p`` writes the merge to stdout (``-p`` is mandatory: the
    flagless form writes the result back into its first argument), and it runs
    on temporary copies — the work tree is never touched. Failure of this probe
    is information, never a second refusal reason.
    """
    import shutil

    if shutil.which("git") is None:
        return None
    # Only a path inside a git work tree gets a git hint at all.
    inside = False
    for action in actions:
        parent = str(Path(action.path).parent)
        try:
            probe = subprocess.run(
                ["git", "-C", parent, "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and probe.stdout.strip() == b"true":
            inside = True
            break
    if not inside:
        return None
    conflicts: list[str] = []
    probed = 0
    for action in actions:
        if action.row is None or not action.row.existed_before:
            continue
        target = Path(action.path)
        current = _read_bytes(target)
        if current is None:
            continue
        ours = blob_bytes(action.row.session_id, action.row.blob_sha256 or "")
        if ours is None:
            continue
        probed += 1
        try:
            with tempfile.TemporaryDirectory(prefix="sherry-merge3way-") as tmp:
                current_file = Path(tmp) / "current"
                ours_file = Path(tmp) / "snapshot"
                base_file = Path(tmp) / "base"
                current_file.write_bytes(current)
                ours_file.write_bytes(ours)
                # No common ancestor is available here: the plan reports whether
                # the two texts CAN be merged, using the snapshot as both sides'
                # ancestor (the standard "did the other writer touch it" test).
                base_file.write_bytes(ours)
                completed = subprocess.run(
                    ["git", "merge-file", "-p", str(current_file), str(base_file), str(ours_file)],
                    capture_output=True,
                    timeout=10,
                )
                if completed.returncode != 0:
                    conflicts.append(action.path)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("merge3way probe failed for {}: {}", action.path, exc)
            continue
    if probed == 0:
        return None
    return {
        "available": True,
        "clean": not conflicts,
        "conflict_paths": conflicts,
        "command": (
            "git merge-file -p <current> <snapshot-before> <your-edits>  # per path, "
            "to review the merge"
        ),
        "note": "based on the work tree as of this check",
    }


def _still_matches(latest: SnapshotRow, target: Path) -> bool:
    """The plan's freshness predicate, re-run just before the write."""
    import hashlib

    current_revision = file_revision(target)
    if current_revision == latest.after_revision:
        return True
    data = _read_bytes(target)
    if data is None:
        return False
    return _hash_guard_allows(len(data)) and hashlib.sha256(data).hexdigest() == latest.after_sha256


def _execute(action: RevertAction, session_id: str) -> tuple[bool, str]:
    """Apply one planned action; returns ``(ok, detail)``.

    The freshness predicate runs again here: the window between plan and apply
    is small but not zero, and a file that moved inside it must be refused
    rather than clobbered — the whole point of the batch rule.
    """
    target = Path(action.path)
    freshness = action.latest or action.row
    if action.action == "delete":
        if freshness is not None and not _still_matches(freshness, target):
            return False, "changed since the plan"
        try:
            os.unlink(target)
            return True, "deleted"
        except FileNotFoundError:
            return True, "already absent"
        except OSError as exc:
            return False, f"delete failed: {safe_error_detail(exc)}"
    if action.action == "restore" and action.row is not None:
        content = blob_bytes(session_id, action.row.blob_sha256 or "")
        if content is None:
            return False, "snapshot_expired"
        if freshness is not None and not _still_matches(freshness, target):
            return False, "changed since the plan"
        try:
            atomic_write_bytes_no_follow(target, content)
        except OSError as exc:
            return False, f"restore failed: {safe_error_detail(exc)}"
        return True, "restored"
    return True, "no change"


def _write_journal(session_id: str, plan: RevertPlan, results: list[dict]) -> None:
    """Best-effort journal of what a revert did (inspection, not recovery)."""
    from config.path import session_file_snapshots_dir

    base = session_file_snapshots_dir(session_id)
    if base is None:
        return
    try:
        base.mkdir(parents=True, exist_ok=True)
        (base / "last_journal.json").write_text(
            json.dumps({"plan": plan.as_dict(), "results": results}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("revert journal write failed for {}: {}", session_id, exc)


def revert_file_changes(
    session_id: str,
    *,
    paths: list[str] | None = None,
    to_tool_call_id: str = "",
    dry_run: bool = False,
) -> dict:
    """Plan (and by default apply) a revert; returns the response payload."""
    earliest_rows, latest_rows = _select_rows(session_id, list(paths or []), to_tool_call_id)
    if not earliest_rows:
        return {
            "success": False,
            "error": "no reversible changes",
            "hint": "This session has no snapshots for the requested scope.",
            "files": [],
        }

    actions = [_classify(session_id, row, latest_rows[row.path]) for row in earliest_rows.values()]
    plan = RevertPlan(actions=actions)

    if dry_run:
        return {"success": True, "dry_run": True, **plan.as_dict()}
    if not plan.safe:
        plan.merge3way = _probe_merge3way(actions)
        return {
            "success": False,
            "error": "external modifications detected; nothing was changed",
            "hint": "Re-read the files and re-apply your change, or resolve the "
            "conflicts by hand (merge3way below, when available).",
            **plan.as_dict(),
        }
    if not plan.changes_anything:
        delete_rows([action.row.id for action in actions if action.row is not None])
        return {"success": True, "files": [action.as_dict() for action in actions]}

    results: list[dict] = []
    consumed: list[int] = []
    for action in actions:
        ok, detail = _execute(action, session_id)
        entry = action.as_dict() | {"ok": ok, "detail": detail}
        results.append(entry)
        if ok and action.row is not None:
            consumed.append(action.row.id)

    _write_journal(session_id, plan, results)
    if all(entry["ok"] for entry in results):
        delete_rows(consumed)
    return {
        "success": all(entry["ok"] for entry in results),
        "files": results,
    }


class FileChangesRevertTool(BaseTool):
    """LLM tool: put files back the way they were, or report why not."""

    name: str = "file_changes_revert"
    description: str = (
        "Revert file changes made by this session's file tools, restoring the "
        "content captured before the write. Refuses as a batch when a file was "
        "changed by anyone else since (nothing is touched in that case)."
    )
    args_schema: type[BaseModel] = FileChangesRevertInput

    @override
    def _run(
        self,
        paths: list[str] | None = None,
        to_tool_call_id: str = "",
        dry_run: bool = False,
        session_id: str = "",
        tool_call_id: str = "",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        session_id = session_id or _extract_session_id(run_manager)
        payload = revert_file_changes(
            session_id,
            paths=paths,
            to_tool_call_id=to_tool_call_id,
            dry_run=dry_run,
        )
        return json.dumps(payload, ensure_ascii=False)

    @override
    async def _arun(
        self,
        paths: list[str] | None = None,
        to_tool_call_id: str = "",
        dry_run: bool = False,
        session_id: str = "",
        tool_call_id: str = "",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        import asyncio

        session_id = session_id or _extract_session_id(run_manager)
        return await asyncio.to_thread(
            lambda: json.dumps(
                revert_file_changes(
                    session_id,
                    paths=paths,
                    to_tool_call_id=to_tool_call_id,
                    dry_run=dry_run,
                ),
                ensure_ascii=False,
            )
        )


def build_file_changes_revert_tool() -> BaseTool:
    """Build the ``file_changes_revert`` tool instance."""
    tool = FileChangesRevertTool()
    tool.handle_tool_error = True
    return tool
