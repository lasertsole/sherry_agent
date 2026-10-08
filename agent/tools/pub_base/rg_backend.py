"""ripgrep execution for the file-search tool: argv, streaming parse, fallbacks.

The engine exists to make one thing true: a scan the Python walk cannot finish
inside its budget can finish inside it. Everything else here is about *not*
trading correctness for that speed —

* **Deterministic order** — ``--sort=path`` (which also disables rg's
  parallelism; measured 1.19s → 2.04s on this repository, still ~8x faster than
  the walk). Without it, paging by ``offset``/``limit`` would be unstable across
  calls and pages could repeat or skip matches.
* **Same envelope** — results are handed back through the tool's own
  :class:`ScanState`, so the match shape, the page cut and the three truncation
  markers cannot drift between the two engines.
* **Same skip rules** — the ``--glob`` exclusions are built from the walker's
  own :data:`SKIP_DIR_NAMES` plus the configured prune dirs, so both engines
  agree on what "skipped" means.
* **No silent incompleteness** — a hard failure (exit >= 2: invalid pattern,
  unreadable directory, malformed glob) returns ``None`` so the caller runs the
  Python search; a timeout returns what was collected, flagged
  ``scan_stop_reason="time_budget"``, because that is what the walk does too and
  a partial answer that says it is partial is worth more than a second opinion
  from an equally budgeted engine.

Context lines are fetched by re-reading the matched files rather than by asking
rg for ``-A/-B``: the walk's context is *whole neighbouring lines* (untruncated),
and the page holds at most ``offset+limit+1`` matches, so this is a handful of
reads in the rare case where ``context > 0``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from agent.tools.pub_base import SKIP_DIR_NAMES, display_path
from agent.tools.pub_base.env_scrub import scrub_env
from agent.tools.pub_base.process_reap import ProcessWatchdog, reap_process
from agent.tools.pub_base.rg_resolver import spawn_failed
from config.features import RIPGREP, TOOLS_TIMEOUTS

__all__ = ["pattern_needs_python", "rg_search", "rg_search_files"]

type _StopReason = Literal["time_budget", "max_matches"]

#: Regex constructs Python supports and ripgrep's default engine does not.
#: A pattern using one of these is handed to the Python walk instead of being
#: silently rejected by rg with exit code 2.
_PYTHON_ONLY_RE = re.compile(r"\\[1-9]|\(\?[=!<]|\(\?>|\(\?\(")

#: Directory names excluded from every rg call: the walker's own skip list plus
#: the configured pseudo-filesystem prune dirs.
_EXCLUDED_DIRS: frozenset[str] = frozenset(SKIP_DIR_NAMES) | frozenset(
    TOOLS_TIMEOUTS["file_tools_search_prune_dirs"]
)

#: Longest match line kept, matching the Python walk's ``line[:500]``.
_MAX_LINE_CHARS = 500


def pattern_needs_python(pattern: str) -> bool:
    """True when *pattern* uses a construct rg's default regex engine lacks."""
    return bool(_PYTHON_ONLY_RE.search(pattern))


def _exclusion_globs() -> list[str]:
    """``--glob`` exclusions equivalent to the walker's pruning.

    A glob without a slash matches a basename at any depth, so
    ``!**/node_modules/**`` prunes that directory wherever it appears, while
    ``!**/.*/**`` prunes every dot-directory (rg is given ``--hidden`` so this
    is the only thing keeping ``.git`` out) without hiding dot *files*, which
    the walker does not skip either.
    """
    globs = ["!**/.*/**"]
    globs.extend(f"!**/{name}/**" for name in sorted(_EXCLUDED_DIRS))
    return globs


def _base_args(
    binary: str, *, json_output: bool = True, include_globs: Sequence[str] = ()
) -> list[str]:
    """Flags shared by both modes.

    Two measured rules live here:

    * every glob travels as ``--glob=<pattern>`` — a bare pattern is read as the
      *search pattern* (or as a path), which rg answers with exit code 2;
    * **includes come before excludes**. rg resolves overlapping globs by
      "last match wins", so an include placed after the exclusions re-admits the
      directories those exclusions pruned: ``--glob=!**/node_modules/**`` then
      ``--glob=*.txt`` returned files inside ``node_modules``, while the reverse
      order pruned them.
    """
    args = [binary, "--no-config", "-i", "--sort=path", "--hidden", "--no-ignore"]
    if json_output:
        args.append("--json")
    args.extend(f"--glob={glob}" for glob in include_globs)
    args.extend(f"--glob={glob}" for glob in _exclusion_globs())
    return args


def _drain_stderr(proc: Any, chunks: list[str], limit: int = 500) -> None:
    """Read stderr to a byte cap on a thread, so a full pipe cannot deadlock us.

    We block on stdout; if rg filled its stderr pipe and stopped, nothing would
    drain it and the scan would hang until the watchdog killed it. The first
    ``limit`` characters are kept for the failure message.
    """
    stream = getattr(proc, "stderr", None)
    if stream is None:
        return
    kept = 0
    try:
        for line in stream:
            if kept < limit:
                chunks.append(line[: limit - kept])
                kept += len(chunks[-1])
    except (OSError, ValueError):
        logger.debug("draining ripgrep stderr failed", exc_info=True)


def _iter_json_lines(proc: Any, watchdog: ProcessWatchdog) -> Iterator[dict[str, Any]]:
    """Yield one parsed ``--json`` frame per line, tolerating junk."""
    assert proc.stdout is not None  # noqa: S101 - PIPE guarantees the stream
    for line in proc.stdout:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            yield json.loads(stripped)
        except json.JSONDecodeError:
            # A truncated final frame when the watchdog fires mid-write.
            if watchdog.fired:
                return
            continue


def _match_entry(frame: dict[str, Any], root: Path) -> tuple[dict[str, Any], Path] | None:
    """Convert one rg match frame into the walk's match shape, or ``None``.

    The resolved path travels beside the entry rather than inside it, so it can
    never leak into the JSON envelope.
    """
    data = frame.get("data") or {}
    path_field = (data.get("path") or {}).get("text")
    if not path_field:
        return None
    line_number = data.get("line_number")
    text = (data.get("lines") or {}).get("text", "")
    resolved = (root / path_field).resolve()
    entry = {
        "path": display_path(resolved),
        "line_number": line_number,
        "content": text.rstrip("\n")[:_MAX_LINE_CHARS],
    }
    return entry, resolved


def _attach_context(matches: list[dict[str, Any]], paths: list[Path], context: int) -> None:
    """Fill ``context_before``/``context_after`` from the matched files."""
    cache: dict[Path, list[str]] = {}
    for entry, path in zip(matches, paths, strict=True):
        lines = cache.get(path)
        if lines is None:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except (OSError, PermissionError):
                lines = []
            cache[path] = lines
        index = (entry["line_number"] or 1) - 1
        entry["context_before"] = lines[max(0, index - context) : index] if lines else []
        entry["context_after"] = lines[index + 1 : index + 1 + context] if lines else []


def _run_ripgrep(
    args: list[str], root: Path, deadline_seconds: float
) -> tuple[list[tuple[dict[str, Any], Path]], str | None]:
    """Run one rg invocation.

    Returns ``(matches, stop_reason)`` where ``stop_reason`` is ``None`` for a
    completed scan, ``"time_budget"`` when the watchdog fired, and
    ``"unavailable"`` when the caller must fall back to the Python walk.
    """
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv list, no shell
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(root),
            env=scrub_env(dict(os.environ)),
        )
    except (OSError, ValueError) as exc:
        logger.warning("ripgrep could not be executed ({}); using the Python search", exc)
        spawn_failed()
        return [], "unavailable"

    stderr_chunks: list[str] = []
    stderr_thread = threading.Thread(target=_drain_stderr, args=(proc, stderr_chunks), daemon=True)
    stderr_thread.start()

    matches: list[tuple[dict[str, Any], Path]] = []
    stop_reason: _StopReason | None = None
    timed_out = False
    try:
        with ProcessWatchdog(deadline_seconds, proc.kill) as dog:
            for frame in _iter_json_lines(proc, dog):
                if frame.get("type") != "match":
                    continue
                parsed = _match_entry(frame, root)
                if parsed is not None:
                    matches.append(parsed)
        timed_out = dog.fired
        if timed_out:
            logger.warning(
                "ripgrep exceeded the {}s scan budget; returning the {} matches collected so far",
                deadline_seconds,
                len(matches),
            )
    finally:
        if proc.poll() is None:
            reap_process(
                proc,
                grace=float(RIPGREP["terminate_grace_seconds"]),
                kill_grace=float(RIPGREP["kill_grace_seconds"]),
            )
        else:
            reap_process(proc)

    stderr_thread.join(timeout=1.0)
    if timed_out:
        stop_reason = "time_budget"
    elif proc.returncode not in (0, 1):
        # 0 = matches, 1 = none, anything else is a hard error: an incomplete
        # result must never be presented as a complete one.
        logger.warning(
            "ripgrep exited with code {} ({}); using the Python search instead",
            proc.returncode,
            "".join(stderr_chunks).strip()[:200] or "no stderr",
        )
        return [], "unavailable"
    return matches, stop_reason


def rg_search(query: Any, state: Any, *, binary: str) -> dict | None:
    """Content search through ripgrep, or ``None`` when the walk must run.

    ``query`` is a :class:`~agent.tools.file_tools.search_scan.SearchQuery`,
    ``state`` a :class:`~agent.tools.file_tools.search_scan.ScanState`.
    """
    page_cap = state.offset + state.limit + 1
    includes = [query.file_glob] if query.file_glob else []
    args = [*_base_args(binary, include_globs=includes)]
    # `-m` is per file; it bounds a single pathological file, while the total
    # cap below is what actually stops the scan (one match past the page proves
    # more exist, which is how the walk decides the same thing).
    args.extend(["-m", str(page_cap)])
    args.extend(["--", query.pattern, "."])

    budget = float(TOOLS_TIMEOUTS["file_tools_search_time_budget_s"])
    matches, stop_reason = _run_ripgrep(args, query.root, budget)
    if stop_reason == "unavailable":
        return None

    if len(matches) >= page_cap:
        # More results exist than the page can hold: keep the page and say so.
        matches = matches[:page_cap]
        state.page_truncated = True
    if len(matches) >= state.max_matches:
        matches = matches[: state.max_matches]
        stop_reason = "max_matches"
    if stop_reason is not None:
        state.stop_reason = stop_reason

    entries = [entry for entry, _ in matches]
    if query.context > 0:
        _attach_context(entries, [path for _, path in matches], query.context)

    result = state.finish("matches", entries)
    if query.context <= 0:
        for entry in result["matches"]:
            entry.pop("context_before", None)
            entry.pop("context_after", None)
    return result


def rg_search_files(query: Any, state: Any, *, binary: str) -> dict | None:
    """File-name search through ``rg --files``, or ``None`` to use the walk."""
    bare_name = query.pattern.split("/")[-1] if "/" in query.pattern else query.pattern
    page_cap = state.offset + state.limit + 1
    args = [
        *_base_args(
            binary,
            json_output=False,
            # The walk matches a basename exactly OR as a substring; one wildcard
            # glob covers both cases. It is an include, so it goes before the
            # exclusions (see _base_args).
            include_globs=[f"*{bare_name}*"],
        ),
        "--files",
    ]

    try:
        proc = subprocess.Popen(  # noqa: S603 - argv list, no shell
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(query.root),
            env=scrub_env(dict(os.environ)),
        )
    except (OSError, ValueError) as exc:
        logger.warning("ripgrep could not be executed ({}); using the Python search", exc)
        spawn_failed()
        return None

    stderr_chunks: list[str] = []
    threading.Thread(target=_drain_stderr, args=(proc, stderr_chunks), daemon=True).start()

    files: list[str] = []
    stop_reason: _StopReason | None = None
    timed_out = False
    try:
        assert proc.stdout is not None  # noqa: S101 - PIPE guarantees the stream
        with ProcessWatchdog(
            float(TOOLS_TIMEOUTS["file_tools_search_time_budget_s"]), proc.kill
        ) as dog:
            for line in proc.stdout:
                rel = line.strip()
                if not rel:
                    continue
                files.append(display_path((query.root / rel).resolve()))
                if len(files) >= page_cap:
                    break
        timed_out = dog.fired
    finally:
        if proc.poll() is None:
            reap_process(
                proc,
                grace=float(RIPGREP["terminate_grace_seconds"]),
                kill_grace=float(RIPGREP["kill_grace_seconds"]),
            )
        else:
            reap_process(proc)

    if timed_out:
        stop_reason = "time_budget"
    elif proc.returncode not in (0, 1):
        logger.warning(
            "ripgrep exited with code {} ({}); using the Python search instead",
            proc.returncode,
            "".join(stderr_chunks).strip()[:200] or "no stderr",
        )
        return None

    if len(files) >= page_cap:
        state.page_truncated = True
    if len(files) >= state.max_matches:
        files = files[: state.max_matches]
        stop_reason = "max_matches"
    if stop_reason is not None:
        state.stop_reason = stop_reason
    return state.finish("files", files)
