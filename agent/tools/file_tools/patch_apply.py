"""Preflight + commit for the multi-file patch mode.

Three phases, mirroring the plan and Codex's apply_patch:

1. :func:`parse_patch` (``patch_parser``) — syntax/path only;
2. :func:`preflight_patch` — resolve every path through the SAME gates the
   single-file path uses, then SIMULATE the whole patch in memory: existence,
   binary refusal, hunk matching (the 9-strategy matcher), destination checks.
   Any refusal aborts the call with no disk change;
3. :func:`commit_patch` — one operation at a time, each under the existing
   ``file_write_lock`` + atomic revision CAS. A commit failure stops the run;
   already-applied operations stay applied and the rest are reported skipped
   (the plan's explicit no-rollback contract).

The per-operation hooks are the single-file path's own: ``ensure_checkpoint``
once, ``_format_py_code`` for ``.py`` targets, ``note_edit`` (advance an
existing read license), ``sync_edited_file`` (LSP) and ``claim_notice_for``.
Patch mode deliberately does NOT add a read-license gate: ``patch_file`` never
had one — its contract is the two-layer revision CAS.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from agent.tools.pub_base import (
    FileBusyError,
    StaleWriteError,
    atomic_write_bytes_no_follow,
    decode_text,
    display_path,
    encode_text,
    file_revision,
    file_write_lock,
    fuzzy_find_and_replace,
    note_edit,
    read_bytes_no_follow,
    resolve_tool_path,
    revision_id,
    safe_error_detail,
    sniff_text_encoding,
)
from agent.tools.pub_base.shadow_git import ensure_checkpoint
from agent.tools.pub_base.evidence_recorder import mark_evidence_stale

from .patch_parser import Hunk, PatchOperation, PatchParseError, parse_patch

__all__ = ["apply_multi_file_patch", "apply_hunks", "closest_lines"]


# ── Hunk application / hints ─────────────────────────────────────────────


@dataclass(frozen=True)
class HunkApplication:
    """The result of applying one file's hunks to its current content."""

    content: str | None
    strategies: tuple[str, ...] = ()
    error: str = ""
    hint: str = ""


def closest_lines(old_text: str, content: str, context: int = 2, max_results: int = 3) -> str:
    """The "Did you mean?" snippets for an unmatched hunk (same as str_replace)."""
    from difflib import SequenceMatcher

    if not old_text or not content:
        return ""
    old_lines = old_text.splitlines()
    content_lines = content.splitlines()
    if not old_lines or not content_lines:
        return ""
    anchor = next((line.strip() for line in old_lines if line.strip()), "")
    if not anchor:
        return ""
    scored = []
    for index, line in enumerate(content_lines):
        stripped = line.strip()
        if not stripped:
            continue
        ratio = SequenceMatcher(None, anchor, stripped).ratio()
        if ratio > 0.3:
            scored.append((ratio, index))
    if not scored:
        return ""
    scored.sort(key=lambda item: -item[0])
    parts: list[str] = []
    seen: set[tuple[int, int]] = set()
    for _, index in scored[:max_results]:
        start = max(0, index - context)
        end = min(len(content_lines), index + len(old_lines) + context)
        key = (start, end)
        if key in seen:
            continue
        seen.add(key)
        parts.append(
            "\n".join(
                f"{start + offset + 1:4d}| {content_lines[start + offset]}"
                for offset in range(end - start)
            )
        )
    return "\n---\n".join(parts) if parts else ""


def apply_hunks(content: str, hunks: tuple[Hunk, ...]) -> HunkApplication:
    """Apply every hunk in order with the shared 9-strategy matcher."""
    current = content
    strategies: list[str] = []
    for hunk in hunks:
        if not hunk.has_change:
            continue
        old_text = hunk.old_text
        new_text = hunk.new_text
        if not old_text.strip():
            return HunkApplication(
                None,
                tuple(strategies),
                "hunk has no context/removed lines to anchor its change",
            )
        new_content, match_count, strategy, error = fuzzy_find_and_replace(
            current, old_text, new_text, False
        )
        if error or match_count == 0:
            hint = closest_lines(old_text, current)
            return HunkApplication(
                None,
                tuple(strategies),
                error or "No match found for the hunk",
                hint,
            )
        current = new_content
        strategies.append(strategy)
    return HunkApplication(current, tuple(strategies))


# ── Preflight ────────────────────────────────────────────────────────────


@dataclass
class ResolvedOperation:
    """One parsed operation resolved to real paths + simulated content."""

    operation: PatchOperation
    root: Path
    path: Path
    encoding: str | None = None
    before: str | None = None
    after: str | None = None
    strategies: tuple[str, ...] = ()
    source_revision: str | None = None
    dest_root: Path | None = None
    dest: Path | None = None


@dataclass
class PreflightReport:
    operations: list[ResolvedOperation] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)


def _error_text(error: str | None) -> str:
    """Render a resolver error JSON as its human message."""
    if not error:
        return "path could not be resolved"
    try:
        payload = json.loads(error)
        if isinstance(payload, dict) and payload.get("error"):
            return str(payload["error"])
    except (ValueError, TypeError):
        pass
    return error


def _read_text(path: Path) -> tuple[str | None, str | None, str]:
    """Read a text file: ``(content, encoding, error)`` (encoding None = binary)."""
    raw, _stat = read_bytes_no_follow(path)
    encoding = sniff_text_encoding(raw)
    if encoding is None:
        return None, None, "file is binary or not UTF-8/UTF-16 text"
    return decode_text(raw, encoding), encoding, ""


def _add_text(operation: PatchOperation) -> str:
    lines = list(operation.content_lines)
    if not lines:
        return ""
    text = "\n".join(lines)
    return text if text.endswith("\n") else text + "\n"


def preflight_patch(operations: list[PatchOperation], session_id: str) -> PreflightReport:
    """Simulate the whole patch; any refusal leaves the disk untouched."""
    report = PreflightReport()
    simulated: dict[Path, str | None] = {}

    def refuse(message: str) -> None:
        report.refusals.append(message)

    for operation in operations:
        root, path, error = resolve_tool_path(
            operation.path, session_id, "apply a patch", mode="write"
        )
        if error is not None or path is None:
            refuse(f"{operation.kind} {operation.path}: {_error_text(error)}")
            continue
        if root is None:
            root = path.parent

        if operation.kind == "add":
            if path in simulated and simulated[path] is not None:
                refuse(f"add {operation.path}: already exists in this patch; use Update File")
                continue
            if path not in simulated and (path.exists() or path.is_symlink()):
                refuse(f"add {operation.path}: already exists; use Update File")
                continue
            content = _add_text(operation)
            simulated[path] = content
            report.operations.append(
                ResolvedOperation(
                    operation=operation,
                    root=root,
                    path=path,
                    before=None,
                    after=content,
                    encoding="utf-8",
                )
            )
            continue

        # update / delete / move start from the simulated view when present.
        if path in simulated:
            current = simulated[path]
            if current is None:
                refuse(f"{operation.kind} {operation.path}: deleted earlier in this patch")
                continue
            stat = None
            encoding: str | None = "utf-8"
        else:
            if not path.exists():
                refuse(f"{operation.kind} {operation.path}: file not found")
                continue
            if path.is_dir():
                refuse(f"{operation.kind} {operation.path}: path is a directory")
                continue
            try:
                raw, stat = read_bytes_no_follow(path)
            except OSError as exc:
                refuse(f"{operation.kind} {operation.path}: {safe_error_detail(exc)}")
                continue
            encoding = sniff_text_encoding(raw)
            if encoding is None:
                refuse(f"{operation.kind} {operation.path}: binary file")
                continue
            current = decode_text(raw, encoding)

        source_revision = revision_id(stat) if stat is not None else None
        if operation.kind == "delete":
            simulated[path] = None
            report.operations.append(
                ResolvedOperation(
                    operation=operation,
                    root=root,
                    path=path,
                    before=current,
                    after=None,
                    source_revision=source_revision,
                )
            )
            continue

        applied = apply_hunks(current, operation.hunks)
        if applied.content is None:
            detail = f"hunk mismatch in {operation.path}: {applied.error}"
            if applied.hint:
                detail += f"\nDid you mean one of these sections?\n{applied.hint}"
            refuse(detail)
            continue

        destination: Path | None = None
        destination_root: Path | None = None
        if operation.kind == "move":
            destination_root, destination, error = resolve_tool_path(
                str(operation.dest_path or ""), session_id, "apply a patch", mode="write"
            )
            if error is not None or destination is None:
                refuse(f"move {operation.path}: {_error_text(error)}")
                continue
            if (destination in simulated and simulated[destination] is not None) or (
                destination not in simulated and (destination.exists() or destination.is_symlink())
            ):
                refuse(f"move {operation.path}: destination {operation.dest_path} already exists")
                continue
            simulated[path] = None
            simulated[destination] = applied.content
        else:
            simulated[path] = applied.content

        report.operations.append(
            ResolvedOperation(
                operation=operation,
                root=root,
                path=path,
                encoding=encoding,
                before=current,
                after=applied.content,
                strategies=applied.strategies,
                source_revision=source_revision,
                dest_root=destination_root,
                dest=destination,
            )
        )
    return report


# ── Commit ───────────────────────────────────────────────────────────────


@dataclass
class OperationResult:
    """The outcome of committing one operation."""

    resolved: ResolvedOperation
    status: str  # completed | failed | skipped
    error: str = ""
    diff: str = ""
    strategy: str = ""

    def payload(self) -> dict[str, object]:
        operation = self.resolved.operation
        body: dict[str, object] = {
            "type": operation.kind,
            "path": operation.path,
            "status": self.status,
        }
        if operation.dest_path:
            body["dest_path"] = operation.dest_path
        if self.strategy:
            body["strategy"] = self.strategy
        if self.diff:
            body["diff"] = self.diff
        if self.error:
            body["error"] = self.error
        return body


def _maybe_format(path: Path, text: str) -> str:
    """Run the existing autopep8 pass for .py targets."""
    if path.suffix != ".py":
        return text
    try:
        from .write_file import _format_py_code

        return _format_py_code(text)
    except Exception:  # noqa: BLE001 - formatting is advisory
        logger.debug("patch mode: Python formatting skipped", exc_info=True)
        return text


def _diff(before: str, after: str, label: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{label}",
            tofile=f"b/{label}",
        )
    )


def _mark_stale(resolved: ResolvedOperation, session_id: str) -> None:
    """Mark each touched path's evidence stale (the decorator only sees file_path).

    Patch mode bypasses the single-file argument the ``@invalidates_evidence``
    decorator reads, so it marks every operation path explicitly — the same
    fail-open call the decorator would make, once per file.
    """
    paths = [resolved.operation.path]
    if resolved.operation.dest_path:
        paths.append(resolved.operation.dest_path)
    for path in paths:
        try:
            mark_evidence_stale(path, session_id)
        except Exception:  # noqa: BLE001 - evidence marking is fail-open
            logger.debug("patch mode: evidence mark skipped for {}", path, exc_info=True)


def _claim(session_id: str, root: Path, path: Path) -> str | None:
    try:
        from agent.tools.coordination_tools import claim_notice_for

        return claim_notice_for(session_id, root, path)
    except Exception:  # noqa: BLE001 - advisory only
        return None


def _sync(path: Path, content: str, session_id: str) -> None:
    try:
        from agent.tools.pub_base.lsp_sync import sync_edited_file

        sync_edited_file(str(path), content, session_id)
    except Exception:  # noqa: BLE001 - advisory only
        logger.debug("patch mode: LSP sync skipped", exc_info=True)


def _commit_add(resolved: ResolvedOperation, session_id: str) -> OperationResult:
    path = resolved.path
    content = _maybe_format(path, resolved.after or "")
    with file_write_lock(path):
        if path.exists() or path.is_symlink():
            raise StaleWriteError(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes_no_follow(
            path, encode_text(content, "utf-8"), expected_revision="absent"
        )
    note_edit(session_id, path, file_revision(path))
    _sync(path, content, session_id)
    return OperationResult(
        resolved,
        "completed",
        diff=_diff("", content, display_path(path, resolved.root)),
    )


def _commit_update(resolved: ResolvedOperation, session_id: str) -> OperationResult:
    path = resolved.path
    with file_write_lock(path):
        raw, stat = read_bytes_no_follow(path)
        encoding = sniff_text_encoding(raw)
        if encoding is None:
            raise OSError("file became binary")
        current = decode_text(raw, encoding)
        applied = apply_hunks(current, resolved.operation.hunks)
        if applied.content is None:
            hint = f"\n{applied.hint}" if applied.hint else ""
            raise OSError(f"{applied.error}{hint}")
        new_content = _maybe_format(path, applied.content)
        atomic_write_bytes_no_follow(
            path, encode_text(new_content, encoding), expected_revision=revision_id(stat)
        )
    note_edit(session_id, path, file_revision(path))
    _sync(path, new_content, session_id)
    return OperationResult(
        resolved,
        "completed",
        diff=_diff(current, new_content, display_path(path, resolved.root)),
        strategy=",".join(applied.strategies),
    )


def _commit_delete(resolved: ResolvedOperation, session_id: str) -> OperationResult:
    path = resolved.path
    with file_write_lock(path):
        if not path.exists():
            raise FileNotFoundError(str(path))
        if resolved.source_revision is not None and file_revision(path) != resolved.source_revision:
            raise StaleWriteError(path)
        path.unlink()
    return OperationResult(
        resolved,
        "completed",
        diff=_diff(resolved.before or "", "", display_path(path, resolved.root)),
    )


def _commit_move(resolved: ResolvedOperation, session_id: str) -> OperationResult:
    source = resolved.path
    destination = resolved.dest
    if destination is None:
        raise OSError("move destination missing")
    raw, stat = read_bytes_no_follow(source)
    encoding = sniff_text_encoding(raw)
    if encoding is None:
        raise OSError("source became binary")
    current = decode_text(raw, encoding)
    applied = apply_hunks(current, resolved.operation.hunks)
    if applied.content is None:
        raise OSError(applied.error)
    content = _maybe_format(destination, applied.content)
    with file_write_lock(destination):
        if destination.exists() or destination.is_symlink():
            raise OSError(
                f"destination already exists: {display_path(destination, resolved.dest_root)}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes_no_follow(
            destination,
            encode_text(content, encoding),
            expected_revision="absent",
        )
    with file_write_lock(source):
        if file_revision(source) != revision_id(stat):
            raise StaleWriteError(source)
        source.unlink()
    note_edit(session_id, destination, file_revision(destination))
    _sync(destination, content, session_id)
    label = display_path(source, resolved.root)
    return OperationResult(
        resolved,
        "completed",
        diff=_diff(current, content, label),
        strategy=",".join(applied.strategies),
    )


def _commit_one(resolved: ResolvedOperation, session_id: str) -> OperationResult:
    kind = resolved.operation.kind
    if kind == "add":
        return _commit_add(resolved, session_id)
    if kind == "update":
        return _commit_update(resolved, session_id)
    if kind == "delete":
        return _commit_delete(resolved, session_id)
    if kind == "move":
        return _commit_move(resolved, session_id)
    raise OSError(f"unsupported operation: {kind}")


def commit_patch(
    resolved_operations: list[ResolvedOperation], session_id: str
) -> list[OperationResult]:
    """Commit in order; the first failure stops the run (no rollback)."""
    results: list[OperationResult] = []
    stopped = False
    for resolved in resolved_operations:
        if stopped:
            results.append(OperationResult(resolved, "skipped"))
            continue
        try:
            result = _commit_one(resolved, session_id)
        except (StaleWriteError, FileBusyError) as exc:
            results.append(
                OperationResult(
                    resolved,
                    "failed",
                    error=(
                        "file changed on disk since the patch was checked; re-read and retry"
                        if isinstance(exc, StaleWriteError)
                        else str(exc)
                    ),
                )
            )
            stopped = True
        except Exception as exc:  # noqa: BLE001 - reported per operation
            results.append(OperationResult(resolved, "failed", error=safe_error_detail(exc)))
            stopped = True
        else:
            results.append(result)
    return results


# ── Tool entry point ─────────────────────────────────────────────────────


def apply_multi_file_patch(patch_text: str, session_id: str) -> str:
    """Parse → preflight → commit; always returns model-facing JSON text."""
    try:
        operations = parse_patch(patch_text)
    except PatchParseError as exc:
        return json.dumps(
            {
                "error": f"Patch syntax error at line {exc.line}: {exc.reason}",
                "changed": False,
            },
            ensure_ascii=False,
        )

    report = preflight_patch(operations, session_id)
    if report.refusals:
        return json.dumps(
            {
                "error": f"Patch rejected ({len(report.refusals)} refusal(s)); no file was changed.",
                "rejected": report.refusals,
                "changed": False,
            },
            ensure_ascii=False,
        )

    ensure_checkpoint(session_id, reason="before_write:apply_patch")
    results = commit_patch(report.operations, session_id)
    notices: list[str] = []
    for result in results:
        if result.status != "completed":
            continue
        _mark_stale(result.resolved, session_id)
        notice = _claim(session_id, result.resolved.root, result.resolved.path)
        if notice:
            notices.append(notice)
        destination = result.resolved.dest
        if destination is not None:
            destination_notice = _claim(
                session_id, result.resolved.dest_root or result.resolved.root, destination
            )
            if destination_notice:
                notices.append(destination_notice)

    failed = next((result for result in results if result.status == "failed"), None)
    payload: dict[str, object] = {
        "success": failed is None,
        "changed": any(result.status == "completed" for result in results),
        "applied": sum(1 for result in results if result.status == "completed"),
        "operations": [result.payload() for result in results],
    }
    if failed is not None:
        payload["error"] = (
            f"Patch stopped at {failed.resolved.operation.kind} {failed.resolved.operation.path}"
        )
    if notices:
        payload["claim_notices"] = notices
    return json.dumps(payload, ensure_ascii=False)
