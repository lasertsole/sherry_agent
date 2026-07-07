"""Python REPL tool with timeout.

Uses subprocess to run code in an isolated Python process. On timeout the
child process is killed cleanly — no thread/memory leakage.
"""
import json
import os
import subprocess
import sys
import textwrap
from typing import Optional
from loguru import logger
from langchain_core.callbacks import CallbackManagerForToolRun, AsyncCallbackManagerForToolRun
from langchain_experimental.tools import PythonREPLTool

PYTHON_REPL_TIMEOUT = 30  # seconds

_REPL_WRAPPER = textwrap.dedent("""\
import sys, json
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
    print(json.dumps({{"out": out, "err": err, "exc": None}}), file=_real_stdout, end="")
except Exception as e:
    out = sys.stdout.getvalue()
    err = sys.stderr.getvalue()
    print(json.dumps({{"out": out, "err": err, "exc": repr(e)}}), file=_real_stdout, end="")
""")


def _run_with_timeout(command: str, timeout: int) -> str:
    """Execute Python code in a subprocess with timeout. Kill on timeout."""
    safe_repr = repr(command)
    script = _REPL_WRAPPER.format(command_repr=safe_repr)

    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
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
        return f"Error: {result['exc']}\n{result['out']}"
    return result["out"] or "(no output)"


class TimedPythonREPLTool(PythonREPLTool):
    """PythonREPLTool with a timeout on each execution.

    Uses subprocess to run code in an isolated Python process and kills it
    on timeout — clean, no leakage, works on Windows.
    """

    def _run(
        self,
        query: str,
        run_manager: Optional[CallbackManagerForToolRun] = None,
    ) -> str:
        return _run_with_timeout(query, PYTHON_REPL_TIMEOUT)

    async def _arun(
        self,
        query: str,
        run_manager: Optional[AsyncCallbackManagerForToolRun] = None,
    ) -> str:
        import asyncio
        return await asyncio.to_thread(_run_with_timeout, query, PYTHON_REPL_TIMEOUT)


def build_python_repl_tool() -> TimedPythonREPLTool:
    tool = TimedPythonREPLTool()
    tool.name = "python_repl"
    tool.handle_tool_error = True
    tool.metadata = {"idempotent": False}
    return tool