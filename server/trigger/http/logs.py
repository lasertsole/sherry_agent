import os
import re
import urllib.parse
from collections import deque
from pathlib import Path
from loguru import logger
from server.trigger.core import app
from config import ROOT_DIR

# Directory where loguru writes its output files (see logs/logger.py).
LOG_DIR = ROOT_DIR / "logs" / "output"


# Matches loguru's file naming scheme: `info_{YYYY-MM-DD}_{pid}.log`.
_LOG_FILENAME_RE = re.compile(r"^(?P<kind>info|error)_\d{4}-\d{2}-\d{2}_(?P<pid>\d+)\.log$")


def _list_log_files(log_dir: Path) -> list[dict]:
    """Return metadata for every uncompressed ``.log`` file under ``log_dir``.

    Only plain ``.log`` files are considered; ``.zip`` archives produced by
    loguru's ``compression="zip"`` are excluded. Entries are sorted newest
    first by modification time.

    Each entry carries an ``is_current`` flag which is ``True`` for the log
    written by the running backend process (identified by the PID embedded in
    the file name matching ``os.getpid()``). Only the current running log can
    be followed live over the WebSocket.
    """
    if not log_dir.is_dir():
        return []

    pid = os.getpid()
    files = []
    for p in log_dir.iterdir():
        if not p.is_file() or p.suffix != ".log":
            continue
        try:
            stat = p.stat()
        except OSError:
            continue
        match = _LOG_FILENAME_RE.match(p.name)
        is_current = bool(match) and match.group("pid") == str(pid)
        files.append({
            "name": p.name,
            "path": str(p),
            "size": stat.st_size,
            "modified": _format_mtime(stat.st_mtime),
            "is_error": p.name.startswith("error"),
            "is_current": is_current,
        })

    files.sort(key=lambda f: f["modified"], reverse=True)
    return files


def _format_mtime(timestamp: float) -> str:
    """Format a unix timestamp as an ISO-8601 string (local time)."""
    import datetime
    return datetime.datetime.fromtimestamp(timestamp).isoformat()


def _resolve_log_path(raw_path: str) -> Path | None:
    """Resolve ``raw_path`` to a real ``.log`` file inside ``LOG_DIR``.

    The path is sent as a URL query parameter, so it arrives URL-encoded
    (e.g. backslashes become ``%5C``). It is unquoted before resolution.

    Returns ``None`` when the path is outside the log directory, is not a
    plain file, or does not exist. Both the candidate and the log directory
    are resolved so symlinks / ``..`` traversal cannot escape the sandbox.
    """
    if not raw_path:
        return None
    try:
        candidate = Path(urllib.parse.unquote(raw_path)).resolve()
    except (OSError, ValueError):
        return None

    log_dir = LOG_DIR.resolve()
    if not candidate.is_relative_to(log_dir):
        return None
    if candidate.suffix != ".log":
        return None
    if not candidate.is_file():
        return None
    return candidate


def _tail_lines(path: Path, lines: int) -> str:
    """Read the trailing ``lines`` lines of ``path`` efficiently.

    Uses a seek-based tail read that walks backwards in fixed-size chunks,
    collecting complete lines into a deque. Falls back to reading the whole
    file when the file is too small or the seek-based approach fails.
    """
    block_size = 8192
    try:
        size = path.stat().st_size
    except OSError:
        return ""

    if size == 0:
        return ""

    collected: deque[str] = deque()
    with open(path, "rb") as f:
        # Read the whole file when it is small enough to be cheap, then keep
        # only the trailing `lines` lines so the requested limit is honored
        # regardless of file size.
        if size <= block_size * 4:
            f.seek(0)
            data = f.read().decode("utf-8", errors="replace")
            split = data.splitlines()
            return "\n".join(split[-lines:])

        # Seek-based tail: walk backwards in blocks, splitting on newlines.
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        buffer = b""
        while pos > 0 and len(collected) < lines:
            read_size = min(block_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size)
            buffer = chunk + buffer
            # Split on newlines; keep the last `lines` complete lines.
            parts = buffer.split(b"\n")
            buffer = parts[0]
            for part in reversed(parts[1:]):
                if len(collected) >= lines:
                    break
                collected.appendleft(part.decode("utf-8", errors="replace"))

        # Handle any remaining partial line at the very start.
        if buffer and len(collected) < lines:
            collected.appendleft(buffer.decode("utf-8", errors="replace"))

    return "\n".join(collected)


@app.get("/logs/files")
async def list_log_files_handler(request):
    """List available uncompressed log files, newest first."""
    files = _list_log_files(LOG_DIR)
    logger.debug(f"Listed log files: count={len(files)}")
    return {"files": files}


@app.get("/logs")
async def read_log_tail_handler(request):
    """Read the trailing ``lines`` lines from a log file.

    Query params:
        path  (required)  absolute path to a ``.log`` file under logs/output/
        lines (optional)  number of trailing lines, default 500, max 5000
    """
    query = request.query_params or {}
    raw_path = query.get("path", "")
    try:
        lines = int(query.get("lines", 500))
    except (TypeError, ValueError):
        lines = 500
    lines = max(1, min(lines, 5000))

    resolved = _resolve_log_path(raw_path)
    if resolved is None:
        logger.warning(f"Log file rejected: path={raw_path}")
        return {"success": False, "error": "Invalid or inaccessible log file path"}, {}, 400

    content = _tail_lines(resolved, lines)
    logger.debug(f"Read log tail: path={resolved}, lines={lines}")
    return {
        "success": True,
        "path": str(resolved),
        "content": content,
        "lines": lines,
    }
