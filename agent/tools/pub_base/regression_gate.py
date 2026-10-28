"""Finish-time re-verification of a flow's recorded evidence.

A cross-session stale row says a verification MAY have been invalidated; it
cannot say whether a sibling's write actually broke the verified behavior. This
gate re-runs the flow's recorded verification commands once, at
``taskflow_finish``:

- pass -> append ``unmark`` for that command (clears a false-positive stale)
- fail -> append ``broken`` for that command (confirmed regression)

Running at finish (not on every announce) keeps the cost to one batch per flow
and sees the merged workspace. Commands are replayed WITHOUT a shell and only
when the executable is a recognized verifier; an unsafe/unparseable/missing
command is skipped, never marked broken. The whole pass is fail-open.
"""

from __future__ import annotations

import asyncio
import os
import posixpath
import re
import shlex
import signal
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from config.features import EVIDENCE_REGRESSION_GATE
from config.path import resolve_default_project_dir
from .env_scrub import scrub_env
from .evidence_ledger import EvidenceLedger, derive_evidence_state
from .path_utils import session_workspace_root

_SESSION_PREFIX = "agent:main:session:"
_SHELL_META = re.compile(r"[;&|<>`$\n\r]")

_ALLOWED_EXECUTABLES = frozenset(
    {
        "pytest",
        "py.test",
        "vitest",
        "jest",
        "ruff",
        "eslint",
        "flake8",
        "pylint",
        "basedpyright",
        "pyright",
        "mypy",
        "tsc",
        "prettier",
        "black",
        "cargo",
        "go",
        "npm",
        "pnpm",
        "yarn",
        "npx",
        "make",
        "cmake",
        "uv",
        "python",
        "python3",
        "py",
    }
)
_PYTHON_MODULES = frozenset({"pytest", "ruff", "mypy", "pyright", "basedpyright", "unittest"})
_PACKAGE_RUNNERS = frozenset({"npm", "pnpm", "yarn"})

#: Formatters rewrite files in place. As a RECORDED verification command they
#: would be replayed by this gate and rewrite a teammate's concurrent edit with
#: no write license, no CAS and no accounting — a one-shot write that bypasses
#: the whole file-tool safety stack. They are replayed only in a
#: report-only form (``--check`` / ``--diff``); a bare invocation is skipped
#: (never failed), so the flow is not pinned by an unusable row.
_FORMATTERS = frozenset({"black", "prettier", "ruff"})
_READ_ONLY_FLAGS = frozenset({"--check", "--diff", "-c"})


def _formatter_is_read_only(executable: str, argv: list[str]) -> bool:
    """Whether a formatter invocation only REPORTS (never rewrites).

    ``ruff`` counts as a formatter only for its ``format`` subcommand (``ruff
    check`` is a linter and is inherently read-only); ``black``/``prettier``
    always rewrite unless told otherwise.
    """
    if executable == "ruff":
        # `ruff check` / `ruff --check ...` lint; `ruff format` rewrites.
        if len(argv) < 2 or argv[1] != "format":
            return True
    return any(token in _READ_ONLY_FLAGS for token in argv[1:])


@dataclass(frozen=True)
class ReverifyResult:
    """One replayed command; ``outcome`` is passed / failed / skipped."""

    command: str
    outcome: str
    exit_code: int | None = None
    detail: str = ""


def _executable_name(token: str) -> str:
    name = Path(token).name.lower()
    return name[:-4] if name.endswith(".exe") else name


#: The sherry.jsonc list key users extend the verifier allowlist with.
#: Also the key the list-settings API accepts (``server/service/sherry_config.py``).
ALLOWED_EXECUTABLES_SETTING = "regression_gate_allowed_executables"


def _get_allowed_executables() -> frozenset[str]:
    """The verifier allowlist: code defaults + the user's sherry.jsonc extras.

    Same shape as the YOLO deny list (``path_utils._get_yolo_deny_paths``): the
    caller owns the defaults, ``load_sherry_list_setting`` contributes the user
    entries, and the merge is read per call so an edited file applies on the
    next replay without a restart. Entries are lower-cased because the check
    compares ``_executable_name`` output.
    """
    from config.sherry_settings import load_sherry_list_setting

    merged: set[str] = set(_ALLOWED_EXECUTABLES)
    for name in load_sherry_list_setting(ALLOWED_EXECUTABLES_SETTING):
        candidate = name.strip().lower()
        if candidate:
            merged.add(candidate)
    return frozenset(merged)


def _is_python_executable(name: str) -> bool:
    """True for ``python`` / ``python3`` / ``python3.13`` (venv bin names)."""
    return name in {"python", "python3", "py"} or bool(re.fullmatch(r"python3\.\d+", name))


def _safe_argv(command: str) -> list[str] | None:
    """Parse a recorded verification command into a no-shell argv.

    Returns None for a command containing shell control operators, a command
    that shlex cannot parse, or an executable outside the verifier allowlist.
    This is deliberately narrower than the terminal tool: the gate replays
    commands automatically, so it accepts only shapes whose first process is a
    known test/lint/build/typecheck tool.
    """
    if not command.strip() or _SHELL_META.search(command):
        return None
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not argv:
        return None
    allowed = _get_allowed_executables()
    executable = _executable_name(argv[0])
    python = _is_python_executable(executable)
    if not python and executable not in allowed:
        return None
    if python:
        if len(argv) < 3 or argv[1] != "-m" or _executable_name(argv[2]) not in _PYTHON_MODULES:
            return None
    elif executable == "uv":
        if len(argv) < 3 or argv[1] != "run":
            return None
        target = _executable_name(argv[2])
        if _is_python_executable(target):
            if len(argv) < 5 or argv[3] != "-m" or _executable_name(argv[4]) not in _PYTHON_MODULES:
                return None
        elif target not in allowed:
            return None
    elif executable in _PACKAGE_RUNNERS:
        if len(argv) < 2 or argv[1] not in {"test", "run", "exec", "x"}:
            return None
        if argv[1] in {"exec", "x"} and (len(argv) < 3 or _executable_name(argv[2]) not in allowed):
            return None
    elif executable == "npx":
        if len(argv) < 2 or _executable_name(argv[1]) not in allowed:
            return None
    # A formatter that would REWRITE is not a verification command: replaying it
    # would overwrite a concurrent edit outside the write-license/CAS stack.
    if executable in _FORMATTERS and not _formatter_is_read_only(executable, argv):
        return None
    if executable in _PACKAGE_RUNNERS and len(argv) >= 3:
        target = _executable_name(argv[2])
        if target in _FORMATTERS and not _formatter_is_read_only(target, argv[2:]):
            return None
    if executable == "npx" and len(argv) >= 2:
        target = _executable_name(argv[1])
        if target in _FORMATTERS and not _formatter_is_read_only(target, argv[1:]):
            return None
    return argv


def _resolve_root(creator_session_key: str, session_keys: Sequence[str]) -> Path:
    """The flow's working root: creator binding, any step binding, then default."""
    for key in [creator_session_key, *session_keys]:
        if not key:
            continue
        root = session_workspace_root(key)
        if root is None and key.startswith(_SESSION_PREFIX):
            root = session_workspace_root(key[len(_SESSION_PREFIX) :])
        if root is not None:
            return root
    return resolve_default_project_dir()


def _normalize_path(value: object, root: Path) -> str:
    """Normalize a declared or recorded path to a project-relative POSIX form."""
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    path = Path(text)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            return path.resolve().as_posix()
    return posixpath.normpath(text)


def _path_covers(declared: str, touched: str) -> bool:
    """True when a declared file/dir and a touched path overlap."""
    if not declared or not touched:
        return False
    return (
        declared == touched
        or touched.startswith(declared.rstrip("/") + "/")
        or declared.startswith(touched.rstrip("/") + "/")
    )


def prioritized_sessions(
    steps: Sequence[dict],
    session_keys: Sequence[str],
    creator_session_key: str = "",
) -> list[str]:
    """Steps whose declared ``file_deps`` another session wrote, in step order.

    Derived at finish time, never persisted: a step's own writes do not count
    (the per-session stale mark covers those); only OTHER sessions' write stale
    events are intersected with the declared dependencies. The result is used
    to replay those steps' commands FIRST, so a capped batch still covers the
    steps a declaration says were affected.
    """
    try:
        keys = list(dict.fromkeys(str(key) for key in session_keys if str(key or "").strip()))
        if not keys:
            return []
        root = _resolve_root(creator_session_key, keys)
        grouped = EvidenceLedger.read_for_keys(keys)
        touched: dict[str, set[str]] = {}
        for key in keys:
            touched[key] = {
                _normalize_path(row.get("file_path"), root)
                for row in (grouped.get(key) or [])
                if row.get("event") == "stale"
                and row.get("scope") != "cross_session"
                and str(row.get("file_path") or "").strip()
            }
        priority: list[str] = []
        for step in steps:
            child = str(step.get("child_session_key") or "")
            raw_deps = step.get("file_deps")
            if not isinstance(raw_deps, list):
                continue
            declared = [
                _normalize_path(dep, root)
                for dep in raw_deps
                if isinstance(dep, str) and dep.strip()
            ]
            if not child or not declared:
                continue
            others = set().union(*(paths for key, paths in touched.items() if key != child))
            if any(_path_covers(dep, path) for dep in declared for path in others):
                priority.append(child)
        if priority:
            logger.info(
                "regression gate: file_deps prioritized {} step(s): {}",
                len(priority),
                priority,
            )
        return list(dict.fromkeys(priority))
    except Exception as exc:  # noqa: BLE001 - a priority hint must never block a finish
        logger.warning("regression gate file_deps derivation failed (fail-open): {}", exc)
        return []


async def _run(argv: list[str], cwd: Path, timeout_s: int) -> tuple[int | None, str]:
    """Run one argv without a shell; returns (exit_code|None, bounded output).

    stdout goes to a temporary file, not a pipe: a noisy test command cannot
    grow the Python heap. Only a head slice is returned for the broken-event
    snippet.
    """
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with tempfile.TemporaryFile() as sink:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=scrub_env(),
                stdout=sink.fileno(),
                stderr=subprocess.STDOUT,
                **kwargs,
            )
        except (FileNotFoundError, OSError) as exc:
            return None, str(exc)
        try:
            await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            _kill(proc)
            await proc.wait()
            return None, f"timed out after {timeout_s}s"
        except asyncio.CancelledError:
            _kill(proc)
            raise
        sink.seek(0)
        output = sink.read(10_001)
    text = output.decode("utf-8", errors="replace")
    if len(text) > 10_000:
        text = text[:10_000] + "…"
    return int(proc.returncode or 0), text


def _kill(proc: Any) -> None:
    """Best-effort kill of the replay process (and its group on POSIX)."""
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _append_broken(session_key: str, command: str, exit_code: int, output: str) -> None:
    EvidenceLedger.append(
        {
            "event": "broken",
            "session_id": session_key,
            "command": command,
            "status": "failed",
            "exit_code": exit_code,
            "output_snippet": output[: int(EVIDENCE_REGRESSION_GATE["max_output_chars"])],
            "scope": "regression_gate",
        }
    )


def _append_unmark(session_key: str, command: str) -> None:
    EvidenceLedger.append(
        {
            "event": "unmark",
            "session_id": session_key,
            "command": command,
            "scope": "regression_gate",
        }
    )


async def reverify_flow_evidence(
    session_keys: Sequence[str],
    creator_session_key: str = "",
    priority_session_keys: Sequence[str] = (),
) -> list[ReverifyResult]:
    """Replay a flow's passed verification commands once; append broken/unmark.

    Evidence is grouped by session. Every unique passed command is replayed at
    most once (``max_reverify_commands`` cap), in the flow's project root. A
    pass appends ``unmark`` only for sessions whose evidence that command had
    marked stale/broken; a failure appends ``broken`` for each affected
    session. Skipped commands (unsafe, missing, timed out) append nothing.

    ``priority_session_keys`` (from declared ``file_deps``) moves those
    sessions' commands to the front; a stable sort keeps ledger order inside
    each group. This changes WHICH commands fit under the cap, never the total
    coverage when the cap is not hit.
    """
    if not EVIDENCE_REGRESSION_GATE["enabled"]:
        return []
    try:
        keys = list(dict.fromkeys(str(key) for key in session_keys if str(key or "").strip()))
        if not keys:
            return []
        grouped = EvidenceLedger.read_for_keys(keys)
        # command -> [(session, record index)]
        targets: dict[str, list[tuple[str, int]]] = {}
        for key in keys:
            records = grouped.get(key) or []
            for index, record in enumerate(records):
                if record.get("event"):
                    continue
                if record.get("status") != "passed":
                    continue
                command = str(record.get("command") or "").strip()
                if command:
                    targets.setdefault(command, []).append((key, index))
        if not targets:
            return []

        limit = max(1, int(EVIDENCE_REGRESSION_GATE["max_reverify_commands"]))
        commands = list(targets)
        priority = set(priority_session_keys)
        if priority:
            if str(EVIDENCE_REGRESSION_GATE["file_deps_mode"]) == "filter":
                filtered = [
                    command
                    for command in commands
                    if any(session in priority for session, _index in targets[command])
                ]
                if filtered:
                    logger.info(
                        "regression gate: file_deps filter narrowed {} command(s) to {}",
                        len(commands),
                        len(filtered),
                    )
                    commands = filtered
            commands.sort(
                key=lambda command: (
                    0 if any(session in priority for session, _index in targets[command]) else 1
                )
            )
        commands = commands[:limit]
        if len(targets) > limit:
            logger.info(
                "regression gate: skipped {} command(s) above the {} cap",
                len(targets) - limit,
                limit,
            )
        root = _resolve_root(creator_session_key, keys)
        if not root.exists():
            logger.warning("regression gate: project root does not exist: {}", root)
            return []
        timeout_s = max(1, int(EVIDENCE_REGRESSION_GATE["reverify_timeout_s"]))

        results: list[ReverifyResult] = []
        for command in commands:
            argv = _safe_argv(command)
            if argv is None:
                logger.debug("regression gate: skipped unsafe command: {}", command[:200])
                results.append(ReverifyResult(command, "skipped", detail="unsafe command"))
                continue
            exit_code, output = await _run(argv, root, timeout_s)
            if exit_code is None:
                results.append(ReverifyResult(command, "skipped", detail=output[:500]))
                continue
            by_session: dict[str, list[int]] = {}
            for session_key, index in targets[command]:
                by_session.setdefault(session_key, []).append(index)
            if exit_code == 0:
                for session_key, indexes in by_session.items():
                    records = grouped.get(session_key) or []
                    if any(any(derive_evidence_state(records, index)) for index in indexes):
                        _append_unmark(session_key, command)
                results.append(ReverifyResult(command, "passed", exit_code=exit_code))
            else:
                for session_key in by_session:
                    _append_broken(session_key, command, exit_code, output)
                results.append(
                    ReverifyResult(command, "failed", exit_code=exit_code, detail=output[:500])
                )
        logger.info(
            "regression gate: {} command(s) replayed ({} passed, {} failed, {} skipped)",
            len(results),
            sum(result.outcome == "passed" for result in results),
            sum(result.outcome == "failed" for result in results),
            sum(result.outcome == "skipped" for result in results),
        )
        return results
    except Exception as exc:  # noqa: BLE001 - the gate must never block a finish by itself
        logger.warning("regression gate failed (fail-open): {}", exc)
        return []


async def reverify_affected_paths(
    session_key: str,
    paths: Sequence[str],
    *,
    max_commands: int = 5,
) -> list[ReverifyResult]:
    """Replay ONE session's passed commands whose evidence covers *paths*.

    The merge-time half of the gate: an isolated merge that renames or reshapes
    a symbol leaves the importing files broken, and the finish-time pass may be
    many turns away. This replays what the session already verified on those
    files now, so the announcement's "affected importers" line carries a
    verdict instead of a guess.

    Safety is identical to the finish pass: whitelisted executables only, no
    shell, one attempt, capped. The anti-loop invariant holds by construction —
    this reads ledger rows and APPENDS events; an appended event never triggers
    a replay or a merge (the merge itself happens once per run, its manifest
    consumed).
    """
    if not EVIDENCE_REGRESSION_GATE["merge_reverify_enabled"]:
        return []
    key = str(session_key or "").strip()
    wanted = [str(path).lstrip("./") for path in paths if str(path or "").strip()]
    if not key or not wanted:
        return []
    try:
        records = EvidenceLedger.read_for_keys([key]).get(key) or []
        targets: list[str] = []
        for record in records:
            if record.get("event") or record.get("status") != "passed":
                continue
            command = str(record.get("command") or "").strip()
            if not command or command in targets:
                continue
            covered = {str(item).lstrip("./") for item in record.get("modified_files") or []}
            if covered & set(wanted):
                targets.append(command)
        if not targets:
            return []
        root = session_workspace_root(key)
        if root is None or not root.exists():
            return []
        timeout_s = max(1, int(EVIDENCE_REGRESSION_GATE["reverify_timeout_s"]))
        results: list[ReverifyResult] = []
        for command in targets[: max(1, int(max_commands))]:
            argv = _safe_argv(command)
            if argv is None:
                results.append(ReverifyResult(command, "skipped", detail="unsafe command"))
                continue
            exit_code, output = await _run(argv, root, timeout_s)
            if exit_code is None:
                results.append(ReverifyResult(command, "skipped", detail=output[:500]))
                continue
            if exit_code == 0:
                _append_unmark(key, command)
                results.append(ReverifyResult(command, "passed", exit_code=exit_code))
            else:
                _append_broken(key, command, exit_code, output)
                results.append(
                    ReverifyResult(command, "failed", exit_code=exit_code, detail=output[:500])
                )
        if results:
            logger.info(
                "merge re-verify: {} command(s) against {} affected path(s) ({} failed)",
                len(results),
                len(wanted),
                sum(result.outcome == "failed" for result in results),
            )
        return results
    except Exception as exc:  # noqa: BLE001 - a merge must never fail on its report
        logger.warning("merge re-verify failed (fail-open): {}", exc)
        return []


__all__ = [
    "ReverifyResult",
    "prioritized_sessions",
    "reverify_affected_paths",
    "reverify_flow_evidence",
]
