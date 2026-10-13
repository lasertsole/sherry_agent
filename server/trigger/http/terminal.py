"""The USER's terminal panel endpoints (toolbox → 终端).

Endpoints:
    GET  /terminal/info?session_id=<sid>
        -> {"success": true, "cwd": "/abs/path", "shell": "/bin/sh"}

        What the panel's prompt shows: the directory commands run in — the
        session's project directory (the selected 工作目录 wins, else the process
        default), never a per-request choice.

    POST /terminal/run  {"session_id": "...", "command": "ls -la"}
        -> {"success": true, "cwd": ..., "command": ..., "exit_code": int,
            "output": str, "truncated": bool, "duration_ms": int}

        One user-typed command, executed in that directory with a scrubbed
        environment, bounded by ``TOOLS_TIMEOUTS["user_terminal_timeout_seconds"]``
        and an output cap. A non-zero ``exit_code`` is a normal answer (a console
        shows it); only a malformed request is a 400.

        400 when ``session_id`` (or the command) is missing / oversized.
"""

from server.service.user_terminal_service import read_terminal_info, run_user_command
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, ok, read_body


@app.get("/terminal/info")
async def terminal_info_handler(request):
    """Return the working directory the user terminal runs commands in."""
    session_id = request.query_params.get("session_id", "") or ""
    try:
        return ok(read_terminal_info(session_id))
    except ValueError as error:
        return bad_request(str(error))


@app.post("/terminal/run")
async def terminal_run_handler(request):
    """Run one user-typed command in the session's project directory."""
    body = read_body(request) or {}
    session_id = str(body.get("session_id") or "").strip()
    command = str(body.get("command") or "")
    try:
        return ok(await run_user_command(session_id, command))
    except ValueError as error:
        return bad_request(str(error))
