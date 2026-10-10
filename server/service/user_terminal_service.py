"""The USER's own terminal: one command in, its output out.

This is the 终端 panel of the toolbox — a console the operator types into, not
the agent's ``terminal`` tool. The differences are deliberate:

* **cwd** is the session's project directory (``current_project_dir``, the same
  resolver every tool and path check uses — the selected 工作目录 wins, otherwise
  the process default), so a user working in a project runs commands there;
* **no approval gate and no sandbox wrapper**: the operator IS the approver, and
  wrapping their shell in the agent's sandbox backend would break interactive
  tools for no security gain on a loopback-only client;
* the environment is still SCRUBBED (``scrub_env``), so a command cannot read the
  API keys the server was started with;
* every run is bounded — one timeout and one output cap per command, with the
  outcome reported (``exit_code`` / ``truncated``) instead of an exception.

One-shot by design: this is a command console, not a PTY. An interactive
full-screen program (vim, top) has nothing to attach to, which the panel says.
"""

from __future__ import annotations

import asyncio
import time

from loguru import logger

from pub.func.message.text_limits import MAX_INLINE_TEXT_CHARS
from config.features import TOOLS_TIMEOUTS
from runtime.session.project_dir import current_project_dir

__all__ = ["read_terminal_info", "run_user_command"]

#: Longest output served back (one console screen is far less; a runaway
#: `yes`-style command must not ship megabytes to the browser).
_MAX_OUTPUT_CHARS = 200_000
#: Longest command accepted (a paste-bomb guard, not a shell limit).
_MAX_COMMAND_CHARS = MAX_INLINE_TEXT_CHARS

_TIMEOUT_S = float(TOOLS_TIMEOUTS["user_terminal_timeout_seconds"])


def read_terminal_info(session_id: str) -> dict[str, str]:
    """What the panel needs to render its prompt: the working directory.

    :param session_id: Session whose project directory the terminal runs in.
    :returns: ``{cwd, shell}`` — the effective root and the shell the runner uses.
    """
    if not session_id or not str(session_id).strip():
        raise ValueError("session_id is required")
    return {"cwd": str(current_project_dir(session_id)), "shell": "/bin/sh"}


async def run_user_command(session_id: str, command: str) -> dict[str, object]:
    """Run ONE user-typed command in the session's project directory.

    :param session_id: Session whose project directory the command runs in.
    :param command: The command line (executed through ``/bin/sh -c``).
    :returns: ``{cwd, command, exit_code, output, truncated, duration_ms}`` —
        ``exit_code`` is the process's own code, and a timed-out command reports
        ``-1`` with the timeout named in ``output``.
    """
    if not session_id or not str(session_id).strip():
        raise ValueError("session_id is required")
    line = str(command or "").strip()
    if not line:
        raise ValueError("command is required")
    if len(line) > _MAX_COMMAND_CHARS:
        raise ValueError(f"command is too long (max {_MAX_COMMAND_CHARS} characters)")

    cwd = str(current_project_dir(session_id))
    from agent.tools.pub_base.env_scrub import scrub_env

    started = time.monotonic()
    exit_code = 0
    truncated = False
    try:
        proc = await asyncio.create_subprocess_shell(
            line,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=scrub_env(),
        )
    except OSError as error:  # a cwd that vanished, a shell that cannot fork
        logger.warning("user terminal: spawn failed: {}", error)
        return {
            "cwd": cwd,
            "command": line,
            "exit_code": -1,
            "output": f"could not start the command: {error}",
            "truncated": False,
            "duration_ms": 0,
        }

    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_S)
        output = raw.decode("utf-8", errors="replace")
        exit_code = int(proc.returncode or 0)
    except TimeoutError:
        # A wedged command must not hold the request thread: kill the process
        # group and report what it managed to print.
        try:
            proc.kill()
            raw, _ = await proc.communicate()
            partial = raw.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - the kill path is best effort
            partial = ""
        output = f"{partial}\n[timed out after {int(_TIMEOUT_S)}s]".strip()
        exit_code = -1
    except asyncio.CancelledError:
        proc.kill()
        raise

    if len(output) > _MAX_OUTPUT_CHARS:
        output = output[:_MAX_OUTPUT_CHARS]
        truncated = True
    return {
        "cwd": cwd,
        "command": line,
        "exit_code": exit_code,
        "output": output,
        "truncated": truncated,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
