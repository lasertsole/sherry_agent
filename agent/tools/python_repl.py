"""Python REPL tool with timeout and sandbox support.

Uses subprocess to run code in an isolated Python process. On timeout the
child process is killed cleanly — no thread/memory leakage.

Sandbox-hardening additions (see .omo/plans/sandbox-hardening.md
lines 647-731):

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
import json
import textwrap
import subprocess
from loguru import logger
from langchain_experimental.tools import PythonREPLTool
from langchain_core.callbacks import CallbackManagerForToolRun, AsyncCallbackManagerForToolRun

from config.path import ROOT_DIR
from agent.tools.pub_base.env_scrub import scrub_env
from agent.tools.pub_base.sandbox import SandboxPolicy, get_backend, read_policy
from agent.tools.pub_base.sandbox_guard import SandboxGuardMixin
from agent.tools.pub_base.schema_utils import class_or_instance_schema

PYTHON_REPL_TIMEOUT = 30  # seconds

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


def _run_with_timeout(command: str, timeout: int, sandbox: bool = True, **_kwargs: Any) -> str:
    """Execute Python code in a subprocess with timeout. Kill on timeout.

    Spawn-point hardening ():

    - ``env=scrub_env()``: the child never sees secret-named variables.
    - ``cwd=str(ROOT_DIR)``: the child never inherits the server's launch dir.
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
    if sandbox:
        policy = read_policy()
        if policy is not SandboxPolicy.OFF:
            backend = get_backend(policy)
            if backend is not None:
                argv, env = backend.wrap(argv, env)
            else:
                logger.warning(
                    "python_repl: sandbox requested but no backend available "
                    f"(policy={policy.value}) — degrading to unsandboxed execution"
                )

    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(ROOT_DIR),
        )
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if proc:
            proc.kill()
            proc.communicate()
        logger.warning("python_repl timed out after {}s", timeout)
        return (
            f"Python REPL execution timed out after {timeout} seconds. "
            "Please simplify your code or try a different approach."
        )

    if proc.returncode != 0:
        return f"Error: subprocess exited with code {proc.returncode}\n{stderr}"

    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        return f"Error: failed to parse output\n{stdout[:500]}"

    if result["exc"]:
        parts = [f"Error: {result['exc']}"]
        if result["out"]:
            parts.append(f"stdout:\n{result['out']}")
        if result["err"]:
            parts.append(f"stderr:\n{result['err']}")
        if result.get("tb"):
            parts.append(f"Traceback:\n{result['tb']}")
        return "\n".join(parts)
    return result["out"] or "(no output)"


class TimedPythonREPLTool(SandboxGuardMixin, PythonREPLTool):
    """PythonREPLTool with a timeout on each execution.

    Uses subprocess to run code in an isolated Python process and kills it
    on timeout — clean, no leakage, works on Windows.

    Note: ``PythonREPLTool`` defines NO explicit ``args_schema``, so
    ``tool_call_schema`` (including ``sandbox``) is derived from the ``_run``
    signature below — the opposite mechanism from terminal's ShellTool
    args_schema subclassing.
    """

    tool_call_schema: ClassVar[Any] = class_or_instance_schema(PythonREPLTool)

    def _run(
        self,
        query: str,
        run_manager: CallbackManagerForToolRun | None = None,
        sandbox: bool = True,
    ) -> str:
        self._deny_sandbox_bypass(sandbox)
        return _run_with_timeout(query, PYTHON_REPL_TIMEOUT, sandbox)

    async def _arun(
        self,
        query: str,
        run_manager: AsyncCallbackManagerForToolRun | None = None,
        sandbox: bool = True,
    ) -> str:
        import asyncio

        self._deny_sandbox_bypass(sandbox)
        return await asyncio.to_thread(_run_with_timeout, query, PYTHON_REPL_TIMEOUT, sandbox)


def build_python_repl_tool() -> TimedPythonREPLTool:
    tool = TimedPythonREPLTool()
    tool.name = "python_repl"
    tool.handle_tool_error = True
    tool.metadata = {"idempotent": False}
    return tool
