"""Python REPL tool with timeout and sandbox support.

Uses subprocess to run code in an isolated Python process. On timeout the
child process is killed cleanly — no thread/memory leakage.

Sandbox-hardening additions (see wiki/sandbox/README.md):

- ``sandbox`` parameter on ``_run``/``_arun``/``_run_with_timeout`` — the
  schema auto-derivation path (PythonREPLTool has NO explicit ``args_schema``,
  the opposite mechanism from terminal's ShellTool subclassing), so the flag
  is picked up from the ``_run`` signature.
- scope/policy guards: ``caller_scope != "main"`` + ``sandbox=False`` is
  denied outright; ``SANDBOX_POLICY=required`` + ``sandbox=False`` is denied
  (main-session human approval wiring arrives with).
- env scrub: the child process always gets ``env=scrub_env()``.
- cwd clamp: the child process always starts in ``ROOT_DIR`` (feasibility gap
  5: inheriting the server's launch directory is a defect).
- OS sandbox wrap: ``sandbox=True`` + policy != OFF + usable backend → the
  interpreter argv is wrapped via ``backend.wrap`` (list exec form); backend
  ``None`` degrades to the direct spawn with one loguru warning line.
"""

from __future__ import annotations

from typing import Any, ClassVar

import sys
import os
from pathlib import Path
import json
import textwrap
import subprocess
from loguru import logger
from langchain_core.tools import BaseTool
from langchain_core.callbacks import CallbackManagerForToolRun, AsyncCallbackManagerForToolRun

from config.path import ROOT_DIR
from config.features import TOOLS_TIMEOUTS
from agent.tools.pub_base import (
    ensure_git_for_writes,
    SessionId,
    _extract_session_id,
    resolve_tool_session_id,
)

from agent.tools.pub_base.env_scrub import scrub_env
from agent.tools.pub_base.process_reap import kill_process_tree
from agent.tools.pub_base.stdout_fd import discard_stdout_tmp, open_stdout_tmp
from agent.tools.pub_base.sandbox import SandboxPolicy, get_backend, read_policy
from agent.tools.pub_base.sandbox_guard import SandboxGuardMixin
from agent.tools.pub_base.schema_utils import class_or_instance_schema
from agent.tools.pub_base.evidence_recorder import record_verification_evidence

#: Injected calling session (see terminal.SessionId for why the runnable-config
#: fallback is never enough in production).

# Bound to the feature registry (single source of truth); name preserved.
PYTHON_REPL_TIMEOUT = TOOLS_TIMEOUTS["python_repl_timeout_seconds"]

#: Temp-file prefix for the child's stdout/stderr (the shared ``.sherry-tmp-``
#: sweep contract): the JSON envelope and the stderr fallback are files, never
#: pipes, so a killed grandchild cannot keep a pipe open.
_REPL_TMP_PREFIX = ".sherry-tmp-repl-"


def _process_group_kwargs() -> dict[str, Any]:
    """Isolate the REPL child so a cancellation can kill its whole tree."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


# Path-hardening assessment — deliberately NO deny-read scan here:
# the wrapper's restricted builtins already omit `open` / `__import__`, so a
# literal `open("/etc/passwd")` fails with NameError before any file is
# touched (locked by tests/agent/tools/pub_base/test_python_repl_tool.py).
# A source-level regex cannot stop the class-introspection escapes that are
# the only remaining route, and would add false positives without raising the
# real barrier — that is the OS sandbox read-shield in sandbox_bwrap.py /
# sandbox_seatbelt.py.
_REPL_WRAPPER = textwrap.dedent("""\
import sys, json, traceback
from io import StringIO

_real_stdout = sys.stdout

# Restricted builtins (safe subset)
__builtins__ = {{
    "True": True, "False": False, "None": None,
    "int": int, "float": float, "str": str, "bool": bool,
    "list": list, "dict": dict, "tuple": tuple, "set": set,
    "len": len, "range": range, "enumerate": enumerate,
    "zip": zip, "map": map, "filter": filter,
    "reversed": reversed, "sorted": sorted,
    "any": any, "all": all, "sum": sum, "min": min, "max": max,
    "abs": abs, "round": round, "pow": pow,
    "print": print, "type": type,
    "isinstance": isinstance, "hasattr": hasattr, "getattr": getattr,
    "dir": dir, "vars": vars, "id": id, "repr": repr,
    "Exception": Exception, "ValueError": ValueError,
    "TypeError": TypeError, "KeyError": KeyError,
    "IndexError": IndexError, "AttributeError": AttributeError,
    "RuntimeError": RuntimeError, "ZeroDivisionError": ZeroDivisionError,
}}

sys.stdout = StringIO()
sys.stderr = StringIO()
try:
    exec({command_repr}, {{"__builtins__": __builtins__}}, {{}})
    out = sys.stdout.getvalue()
    err = sys.stderr.getvalue()
    print(json.dumps({{"out": out, "err": err, "exc": None, "tb": None}}), file=_real_stdout, end="")
except Exception as e:
    out = sys.stdout.getvalue()
    err = sys.stderr.getvalue()
    tb = traceback.format_exc()
    print(json.dumps({{"out": out, "err": err, "exc": repr(e), "tb": tb}}), file=_real_stdout, end="")
""")


def _resolve_session_cwd(run_manager: Any, session_id: str = "") -> str | None:
    """The session's project directory for this call, or ``None`` (-> ROOT_DIR).

    Read per call: the REPL tool is a process-level singleton, so resolving the
    root at construction time would pin every session to the first caller's
    directory.
    """
    from agent.tools.pub_base import session_workspace_root

    # The injected state is the production channel; the runnable-config lookup
    # below it stays as a fallback for direct/test callers (it is empty at runtime).
    bound = session_workspace_root(resolve_tool_session_id(session_id, run_manager))
    return str(bound) if bound is not None else None


def _run_with_timeout(
    command: str,
    timeout: int,
    sandbox: bool = True,
    cwd: str | None = None,
    writable_roots: list[Path] | None = None,
    session_id: str = "",
    proc_out: list[subprocess.Popen[bytes]] | None = None,
    **_kwargs: Any,
) -> tuple[str, bool]:
    """Execute Python code in a subprocess with timeout. Kill on timeout.

    Spawn-point hardening ():

    - ``env=scrub_env()``: the child never sees secret-named variables.
    - ``cwd``: the session's project directory when given (resolved per call by
      the caller — the tool is a process-level singleton, so caching a root here
      would freeze every session onto the first caller's directory); otherwise
      ``ROOT_DIR``, the pre-project-binding behaviour. Either way the child
      never inherits the server's launch dir.
    - ``sandbox=True`` + policy != OFF: ``get_backend`` resolves the OS
      sandbox — a usable backend wraps the interpreter argv (list exec form,
      no shell); ``None`` degrades to the direct unsandboxed spawn with one
      loguru warning line. ``REQUIRED`` + unavailable backend raises
      ``RuntimeError`` (surfaced as a tool error via ``handle_tool_error``).

    Extra ``**kwargs`` are absorbed for forward compatibility (/9 may
    add flags) and deliberately ignored.
    """
    safe_repr = repr(command)
    script = _REPL_WRAPPER.format(command_repr=safe_repr)

    env = scrub_env()
    argv: list[str] = [sys.executable, "-c", script]
    ran_sandboxed = False
    if sandbox:
        policy = read_policy()
        if policy is not SandboxPolicy.OFF:
            backend = get_backend(policy)
            if backend is not None:
                # The sandbox's write surface: the session's project dir (the
                # same root the cwd came from), falling back to the process root
                # when no session is in scope.
                roots = writable_roots or [Path(cwd or ROOT_DIR)]
                argv, env = backend.wrap(argv, env, roots)
                ran_sandboxed = True
            else:
                logger.warning(
                    "python_repl: sandbox requested but no backend available "
                    f"(policy={policy.value}) — degrading to unsandboxed execution"
                )

    proc: subprocess.Popen[bytes] | None = None
    # fd-direct-write (the terminal tool's contract): the caller's code may fork
    # grandchildren that inherit the write end — with pipes, an unbounded
    # ``communicate()`` after a kill would block until those grandchildren exit
    # (forever for `os.system("sleep 9999")`). The envelope and the stderr
    # fallback land in temp files, so ``wait()`` depends only on this child and
    # the timeout path uses the bounded ``reap_process`` escalation.
    stdout_path, stdout_handle = open_stdout_tmp(session_id, prefix=_REPL_TMP_PREFIX)
    stderr_path, stderr_handle = open_stdout_tmp(session_id, prefix=_REPL_TMP_PREFIX)
    stdout = stderr = ""
    try:
        try:
            proc = subprocess.Popen(
                argv,
                stdout=stdout_handle,
                stderr=stderr_handle,
                env=env,
                cwd=cwd or str(ROOT_DIR),
                **_process_group_kwargs(),
            )
            if proc_out is not None:
                proc_out.append(proc)
            stdout_handle.close()
            stderr_handle.close()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if proc:
                kill_process_tree(proc, grace=1.0)
            logger.warning("python_repl timed out after {}s", timeout)
            return (
                f"Python REPL execution timed out after {timeout} seconds. "
                "Please simplify your code or try a different approach.",
                ran_sandboxed,
            )
        stdout = stdout_path.read_bytes().decode("utf-8", errors="replace")
        stderr = stderr_path.read_bytes().decode("utf-8", errors="replace")
    finally:
        discard_stdout_tmp(None, stdout_path)
        discard_stdout_tmp(None, stderr_path)

    if proc.returncode != 0:
        return f"Error: subprocess exited with code {proc.returncode}\n{stderr}", ran_sandboxed

    try:
        result = json.loads(stdout)
        logger.debug("python_repl: result JSON decode failed", exc_info=True)
    except json.JSONDecodeError:
        return f"Error: failed to parse output\n{stdout[:500]}", ran_sandboxed

    if result["exc"]:
        parts = [f"Error: {result['exc']}"]
        if result["out"]:
            parts.append(f"stdout:\n{result['out']}")
        if result["err"]:
            parts.append(f"stderr:\n{result['err']}")
        if result.get("tb"):
            parts.append(f"Traceback:\n{result['tb']}")
        return "\n".join(parts), ran_sandboxed
    return result["out"] or "(no output)", ran_sandboxed


class TimedPythonREPLTool(SandboxGuardMixin, BaseTool):
    """A Python REPL that runs each call in a sandboxed, time-limited subprocess.

    Re-based on ``BaseTool``: the former ``PythonREPLTool`` parent came from the
    archived ``langchain-experimental`` and contributed nothing this tool did
    not already replace — its ``_run`` ran in-process through ``python_repl``
    (which upstream itself now calls superseded by sandboxes), while this tool
    spawns a subprocess through the repo's own sandbox.
    """

    name: str = "python_repl"
    # The DESCRIPTION is upstream PythonREPLTool's sentence verbatim: the model
    # has always been shown it, and the migration must not change the surface.
    description: str = (
        "A Python shell. Use this to execute python commands. Input should be a "
        "valid python command. If you want to see the output of a value, you "
        "should print it out with `print(...)`."
    )

    # ``args_schema`` stays unset: the tool_call_schema (query + sandbox) is
    # derived from the ``_run`` signature below. Pinned byte-for-byte by
    # ``tests/agent/tools/test_tool_schema_goldens.py``.
    #
    #: CLASS-level access keeps working through the same descriptor the terminal
    #: tool uses (langchain-core's ``tool_call_schema`` is an instance property).
    tool_call_schema: ClassVar[Any] = class_or_instance_schema(BaseTool)

    def _run(
        self,
        query: str,
        run_manager: CallbackManagerForToolRun | None = None,
        sandbox: bool = True,
        session_id: SessionId = "",
    ) -> str:
        self._deny_sandbox_bypass(sandbox)
        # A bound project directory must be a git repository before code runs:
        # python can write files (open(path, 'w')) without touching the file
        # tools, so the gate cannot live only in write_file / patch_file.
        git_ok, git_error = ensure_git_for_writes(resolve_tool_session_id(session_id, run_manager))
        if not git_ok:
            return git_error
        session_cwd = _resolve_session_cwd(run_manager, session_id)
        result, ran_sandboxed = _run_with_timeout(
            query,
            PYTHON_REPL_TIMEOUT,
            sandbox,
            session_cwd,
            [Path(session_cwd)] if session_cwd else None,
            session_id=resolve_tool_session_id(session_id, run_manager),
        )
        record_verification_evidence(
            query, result, _extract_session_id(run_manager), sandboxed=ran_sandboxed
        )
        return result

    async def _arun(
        self,
        query: str,
        run_manager: AsyncCallbackManagerForToolRun | None = None,
        sandbox: bool = True,
        session_id: SessionId = "",
    ) -> str:
        import asyncio

        self._deny_sandbox_bypass(sandbox)
        # Same gate as the sync path (see its comment).
        git_ok, git_error = ensure_git_for_writes(resolve_tool_session_id(session_id, run_manager))
        if not git_ok:
            return git_error
        session_cwd = _resolve_session_cwd(run_manager, session_id)
        proc_out: list[subprocess.Popen[bytes]] = []
        try:
            result, ran_sandboxed = await asyncio.to_thread(
                _run_with_timeout,
                query,
                PYTHON_REPL_TIMEOUT,
                sandbox,
                session_cwd,
                [Path(session_cwd)] if session_cwd else None,
                proc_out=proc_out,
            )
        except asyncio.CancelledError:
            # to_thread cannot cancel the blocking wait, so the child would
            # outlive the turn. Kill its whole group explicitly first.
            if proc_out:
                kill_process_tree(proc_out[0], grace=1.0)
            return "Python REPL execution was cancelled (process tree killed)."
        record_verification_evidence(
            query, result, _extract_session_id(run_manager), sandboxed=ran_sandboxed
        )
        return result


def build_python_repl_tool() -> TimedPythonREPLTool:
    tool = TimedPythonREPLTool()
    tool.handle_tool_error = True
    tool.metadata = {"idempotent": False, "side_effect_scope": "system"}
    return tool
