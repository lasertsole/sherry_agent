"""Fail-open auto-recording of verification evidence.

Terminal / python_repl results that look like verification commands
(test / lint / build / typecheck / format) become append-only ledger rows, and
file edits append a stale event. Every entry point swallows its own errors: a
ledger write must never break the tool that produced the result.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from loguru import logger

from config.features import EVIDENCE_LEDGER
from agent.tools.todolist.evidence_ledger import EvidenceLedger

_EXIT_CODE_RE = re.compile(r"^(?:Exit code|Error: subprocess exited with code) (-?\d+)")
_FAILURE_PREFIXES = (
    "Error:",
    "Terminal command timed out",
    "Terminal command was cancelled",
    "Python REPL execution timed out",
)
_OUTPUT_SNIPPET_CHARS = 500


def classify_verification_command(command: str) -> str | None:
    """Return the verification kind for ``command``, or None if it is not one."""
    lowered = command.lower().strip()
    for kind, patterns in EVIDENCE_LEDGER["verify_commands"].items():
        if any(pattern in lowered for pattern in patterns):
            return kind
    return None


def _parse_exit_code(result: str) -> int:
    """Derive the exit code from a tool result string (0 when unspecified)."""
    match = _EXIT_CODE_RE.match(result)
    if match:
        return int(match.group(1))
    if result.startswith(_FAILURE_PREFIXES):
        return 1
    return 0


def record_verification_evidence(command: str, result: str, session_id: str) -> None:
    """Append one evidence row for a recognized verification command.

    Fail-open: an unclassified command or any ledger write error leaves the tool
    result untouched and only logs.
    """
    try:
        kind = classify_verification_command(command)
        if kind is None:
            return
        exit_code = _parse_exit_code(result)
        EvidenceLedger.for_session(session_id).append(
            kind=kind,
            command=command,
            exit_code=exit_code,
            status="passed" if exit_code == 0 else "failed",
            output_snippet=result[:_OUTPUT_SNIPPET_CHARS],
        )
    except Exception as exc:
        logger.warning("evidence auto-record failed: {}", exc)


def mark_evidence_stale(file_path: str, session_id: str) -> None:
    """Append a stale event for ``file_path`` after an edit (fail-open)."""
    try:
        EvidenceLedger.for_session(session_id).mark_stale_for_path(file_path)
    except Exception as exc:
        logger.warning("evidence stale mark failed: {}", exc)


def mark_evidence_stale_for_paths(paths: Iterable[str], session_id: str) -> None:
    """Batch form of :func:`mark_evidence_stale` (fail-open, one row per path)."""
    for path in paths:
        mark_evidence_stale(str(path), session_id)


def invalidates_evidence(
    path_arg: str = "file_path",
    session_arg: str = "session_id",
):
    """Decorate a file-write handler so its path's evidence goes stale.

    The write paths used to place the ``mark_evidence_stale`` call by hand —
    and write_file placed it twice (success and failure) while its exception
    branch was easy to miss on a new write path. Wrapping the handler instead
    fires on EVERY outcome the handler can have: a returned error string, a
    raised exception, or a completed write. Fail-open: the mark itself never
    raises, and an unresolvable argument (a direct caller that passes the path
    positionally and the session by keyword) only skips the mark.
    """
    import functools
    import inspect

    def decorate(fn):
        signature = inspect.signature(fn)

        def _resolve(args, kwargs) -> tuple[str, str]:
            try:
                bound = signature.bind_partial(*args, **kwargs)
            except TypeError:
                return "", ""
            return str(bound.arguments.get(path_arg) or ""), str(
                bound.arguments.get(session_arg) or ""
            )

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                try:
                    return await fn(*args, **kwargs)
                finally:
                    path, session_id = _resolve(args, kwargs)
                    if path:
                        mark_evidence_stale(path, session_id)

            return async_wrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            finally:
                path, session_id = _resolve(args, kwargs)
                if path:
                    mark_evidence_stale(path, session_id)

        return wrapper

    return decorate
