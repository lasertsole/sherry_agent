"""PTC execution orchestration.

:func:`run_ptc` wires the pieces together: start the loopback RPC server,
generate the ``sherry_tools.py`` stub, write a wrapper script that executes the
model-authored code under restricted builtins, spawn the child process with a
scrubbed environment, then capture and truncate its output.

Safety invariants:

- The child gets ``env=scrub_env()`` (no ``*_KEY`` / ``*_TOKEN`` / secrets) plus
  only ``PYTHONPATH=<tmpdir>`` and a one-time RPC token. The token is generated
  per run (``secrets.token_urlsafe``), lives only in memory, and is passed to
  the child through the ``SHERRY_PTC_RPC_TOKEN`` environment variable — never
  written to ``sherry_tools.py`` or any other file.
- The child starts in its own process group; on timeout the whole group is
  SIGKILLed, so no orphan process (or grandchild) survives.
- stdout/stderr are truncated to the configured byte caps.
- The child argv is wrapped by the OS-native sandbox backend (the same
  ``pub_base/sandbox.py`` backends ``terminal`` and ``python_repl`` use) when a
  backend is available: ``required`` unavailable ⇒ the run is refused
  (fail-closed); ``auto`` unavailable ⇒ one loguru warning then an unsandboxed
  degrade; ``off`` ⇒ no wrap.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import signal
import subprocess  # noqa: S404 - intentional isolated child spawn
import sys
import tempfile
import threading
from typing import Any

from loguru import logger

from config.path import ROOT_DIR
from agent.tools.pub_base.env_scrub import scrub_env
from agent.tools.pub_base.sandbox import SandboxPolicy, get_backend, read_policy

from .builtins import render_allowed_modules, render_restricted_builtins
from .rpc_server import PtcRpcServer
from .stub_generator import PTC_RPC_TOKEN_ENV, generate_stub, tool_specs_from_base_tools

#: Prefix for the per-call temporary directory (also used by orphan checks).
PTC_TMP_PREFIX = "sherry_ptc_"


class PtcSandboxUnavailableError(RuntimeError):
    """``SANDBOX_POLICY=required`` and no OS backend: refuse the run."""


def _resolve_sandboxed_argv(
    argv: list[str], env: dict[str, str]
) -> tuple[list[str], dict[str, str]]:
    """Wrap ``argv`` with the OS-native backend per the ``SANDBOX_POLICY`` mapping.

    PTC never runs in the main session and exposes no ``sandbox`` bypass flag,
    so it always requests sandboxing — identical to a ``sandbox=True`` call:

    - ``off`` → no wrap, backend never probed.
    - ``auto`` → a usable backend wraps the argv; an unavailable one degrades to
      the direct unsandboxed spawn with exactly one loguru warning.
    - ``required`` → a usable backend wraps the argv; an unavailable one raises
      :class:`PtcSandboxUnavailableError` so the run is refused (fail-closed).
    """
    policy = read_policy()
    if policy is SandboxPolicy.OFF:
        return argv, env
    try:
        backend = get_backend(policy)
    except RuntimeError as exc:
        raise PtcSandboxUnavailableError(
            "execute_code refused: SANDBOX_POLICY=required but no OS sandbox backend is "
            f"available ({exc}). Install bubblewrap (Linux) or use macOS Seatbelt, or set "
            "SANDBOX_POLICY=auto to allow the unsandboxed degrade path."
        ) from exc
    if backend is None:
        logger.warning(
            "execute_code: sandbox requested but no backend available "
            f"(policy={policy.value}) — degrading to unsandboxed execution"
        )
        return argv, env
    return backend.wrap(argv, env)


_SCRIPT_TEMPLATE = """\
import json
import sys
import traceback
from io import StringIO

_real_stdout = sys.stdout
_real_stderr = sys.stderr

import builtins as _builtins

_real_import = _builtins.__import__

_ALLOWED_MODULES = __ALLOWED_MODULES__


def _restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".")[0]
    if root not in _ALLOWED_MODULES:
        raise ImportError("import of %r is not allowed in PTC" % (name,))
    return _real_import(name, globals, locals, fromlist, level)


_restricted_builtins = __RESTRICTED_BUILTINS__
_restricted_builtins["__import__"] = _restricted_import

_USER_CODE = __USER_CODE__

sys.stdout = StringIO()
sys.stderr = StringIO()
_globals = {"__builtins__": _restricted_builtins, "__name__": "__main__"}
try:
    exec(compile(_USER_CODE, "<ptc>", "exec"), _globals, _globals)
    _out = sys.stdout.getvalue()
    _err = sys.stderr.getvalue()
    sys.stdout = _real_stdout
    sys.stderr = _real_stderr
    print(json.dumps({"out": _out, "err": _err, "exc": None}), file=_real_stdout, end="")
except BaseException as _e:
    _out = sys.stdout.getvalue()
    _err = sys.stderr.getvalue()
    sys.stdout = _real_stdout
    sys.stderr = _real_stderr
    print(
        json.dumps({"out": _out, "err": _err, "exc": repr(_e), "tb": traceback.format_exc()}),
        file=_real_stdout,
        end="",
    )
"""


def build_child_script(user_code: str) -> str:
    """Render the wrapper script that runs ``user_code`` under restricted builtins."""
    return (
        _SCRIPT_TEMPLATE.replace("__ALLOWED_MODULES__", render_allowed_modules())
        .replace("__RESTRICTED_BUILTINS__", render_restricted_builtins())
        .replace("__USER_CODE__", repr(user_code))
    )


def build_child_env(tmpdir: str, token: str) -> dict[str, str]:
    """Build the scrubbed child environment: stub dir on PYTHONPATH + RPC token."""
    env = scrub_env()
    env["PYTHONPATH"] = tmpdir
    env["PYTHONUNBUFFERED"] = "1"
    env[PTC_RPC_TOKEN_ENV] = token
    return env


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Kill the child and any grandchildren it spawned."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except (AttributeError, ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...[truncated {len(value) - limit} bytes]"


def _parse_envelope(stdout: str, stderr: str) -> tuple[str, str, str | None]:
    """Return ``(user_stdout, user_stderr, error_or_None)``."""
    payload: Any = None
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if isinstance(payload, dict) and "out" in payload:
        out = str(payload.get("out") or "")
        err = str(payload.get("err") or "")
        exc = payload.get("exc")
        tb = payload.get("tb") or ""
        if exc:
            detail = str(tb).strip() or str(exc)
            if err.strip():
                detail = f"{detail}\n[stderr]\n{err}"
            return out, err, detail
        return out, err, None
    # No envelope (e.g. interpreter-level failure) — surface raw stderr.
    return stdout, stderr, (stderr.strip() or None)


def _result(
    status: str,
    output: str,
    error: str,
    exit_code: int,
    tool_calls: int,
) -> str:
    return json.dumps(
        {
            "status": status,
            "output": output,
            "error": error,
            "exit_code": exit_code,
            "tool_calls_made": tool_calls,
        },
        ensure_ascii=False,
    )


async def run_ptc(
    code: str,
    tools_map: dict[str, Any],
    session_id: str,
    config: Any,
    *,
    cwd: str | None = None,
) -> str:
    """Execute ``code`` in an isolated child process with RPC tool access.

    Returns a JSON string describing the outcome. ``tools_map`` maps tool name
    to a live BaseTool; ``config`` is the :data:`config.features.PTC` mapping.
    """
    max_stdout = int(config["ptc_max_stdout_bytes"])
    max_stderr = int(config["ptc_max_stderr_bytes"])
    timeout = int(config["ptc_timeout_seconds"])

    loop = asyncio.get_running_loop()
    token = secrets.token_urlsafe(32)
    server = PtcRpcServer(
        tools_map,
        loop,
        int(config["ptc_max_tool_calls"]),
        session_id,
        tool_call_timeout=float(timeout),
        token=token,
    )
    host, port = server.start()

    tmpdir = tempfile.mkdtemp(prefix=PTC_TMP_PREFIX)
    serve_thread = threading.Thread(target=server.serve_forever, name="ptc-rpc", daemon=True)
    proc: subprocess.Popen[str] | None = None
    try:
        stubs = tool_specs_from_base_tools(tools_map.values())
        with open(os.path.join(tmpdir, "sherry_tools.py"), "w", encoding="utf-8") as fh:
            fh.write(generate_stub(host, port, stubs))

        script_path = os.path.join(tmpdir, "script.py")
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(build_child_script(code))

        env = build_child_env(tmpdir, token)
        serve_thread.start()

        argv: list[str] = [sys.executable, script_path]
        try:
            argv, env = _resolve_sandboxed_argv(argv, env)
        except PtcSandboxUnavailableError as exc:
            return _result("sandbox_unavailable", "", str(exc), -1, 0)

        proc = subprocess.Popen(  # noqa: S603 - argv is fully controlled
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=cwd or str(ROOT_DIR),
            start_new_session=True,
        )

        try:
            # communicate() blocks; run it off-loop so the RPC thread can still
            # dispatch tool calls onto this event loop while the child runs.
            stdout, stderr = await asyncio.to_thread(proc.communicate, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            stdout, stderr = await asyncio.to_thread(proc.communicate)
            return _result(
                "timeout",
                _truncate(stdout or "", max_stdout),
                _truncate(stderr or "", max_stderr),
                int(proc.returncode or -1),
                server.calls_made,
            )

        out, _err, error = _parse_envelope(stdout or "", stderr or "")
        exit_code = int(proc.returncode or 0)
        if server.budget_exhausted:
            status = "budget_exceeded"
        elif error is not None or exit_code != 0:
            status = "error"
        else:
            status = "ok"
        return _result(
            status,
            _truncate(out, max_stdout),
            _truncate(error or "", max_stderr),
            exit_code,
            server.calls_made,
        )
    finally:
        server.stop()
        serve_thread.join(timeout=1.0)
        shutil.rmtree(tmpdir, ignore_errors=True)
        if proc is not None and proc.poll() is None:
            logger.warning("PTC child process survived cleanup — killing")
            _kill_process_tree(proc)
