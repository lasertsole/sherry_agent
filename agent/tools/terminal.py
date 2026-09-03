"""Terminal tool with sandbox, blacklist, timeout, and env scrubbing.

Sandbox-hardening Task 6 additions (see .omo/plans/sandbox-hardening.md
lines 554-568):

- ``SafeShellInput`` subclass: the ``sandbox`` flag is exposed to the LLM via
  an explicit ``args_schema`` override (ShellTool declares args_schema
  explicitly, so signature-only changes never propagate — the schema must be
  subclassed). The ClassVar ``_ClassOrInstanceSchema`` descriptor lets
  ``tool_call_schema`` be read from the class too (langchain_core 1.4.7
  defines it as a bare instance ``@property``; pattern mirrored from
  python_repl.py Task 7).
- ``sandbox`` parameter on ``_run``/``_arun`` (langchain injects schema fields
  by name), with scope/policy guards: ``caller_scope != "main"`` +
  ``sandbox=False`` is denied outright; ``SANDBOX_POLICY=required`` +
  ``sandbox=False`` is denied (main-session human approval wiring arrives
  with Task 8 — no ``interrupt()`` here).
- ``DANGEROUS_COMMAND_REGEX``: regex blacklist over the ``" && "``-joined
  command string (re.IGNORECASE), replacing the old element-exact substring
  set that let ``["echo ok", "rm -rf /"]`` slip through. On hit a
  ``ToolException`` is raised with the historical refusal message format
  (``handle_tool_error=True`` surfaces it as a tool error).
- env scrub: ``env = scrub_env()`` reaches BOTH sync and async spawns,
  unconditionally (even for ``sandbox=False`` calls).
- OS sandbox wrap: ``sandbox=True`` + policy != OFF + usable backend → the
  command is exec'd in list form via ``backend.wrap(["/bin/sh", "-c", ...])``
  (semantically identical to POSIX ``shell=True``); backend ``None``
  (Windows / unavailable) degrades to the byte-identical pre-existing path
  (str join + ``shell=True`` / ``create_subprocess_shell``) with ONLY
  ``env=`` added, plus one loguru warning line for the AUTO degrade.
- REQUIRED + unavailable backend: ``get_backend``'s RuntimeError is wrapped
  into a ``ToolException`` so ``handle_tool_error=True`` surfaces it.
"""

from __future__ import annotations

import locale
import asyncio
import re
import subprocess
from typing import Any, ClassVar
from loguru import logger
from pydantic import BaseModel, Field
from typing import override
from config import ROOT_DIR
from langchain_community.tools import ShellTool
from langchain_community.tools.shell.tool import ShellInput
from langchain_core.callbacks import CallbackManagerForToolRun, AsyncCallbackManagerForToolRun
from langchain_core.tools import ToolException

from agent.tools.pub_base.env_scrub import scrub_env
from agent.tools.pub_base.sandbox import SandboxPolicy, get_backend, read_policy

TERMINAL_TIMEOUT = 30  # seconds

# Historical refusal message format (terminal.py Task 5 snapshot); now RAISED
# as a ToolException instead of returned, so handle_tool_error=True routes it
# through the error ToolMessage channel.
_BLOCKED_MESSAGE = "Blocked: unsafe command."

# Regex blacklist over the " && "-joined command string (plan line 563).
# Supersedes the old element-exact BLACKLIST set: "rm -rf /", "mkfs",
# "shutdown", "reboot" are all covered, plus the joined/chained variants the
# exact matcher missed (["echo ok", "rm -rf /"] was the Task 5 defect).
DANGEROUS_COMMAND_REGEX = re.compile(
    "|".join(
        [
            r"rm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r|-[a-z]*r)\s+[~-]?/?\s*$",
            r"rm\s+-[a-z]*r",
            r"mkfs",
            r"shutdown",
            r"reboot",
            r"(\||&&|;)\s*(rm|shutdown|reboot|mkfs)",
        ]
    ),
    re.IGNORECASE,
)


class SafeShellInput(ShellInput):
    """ShellInput + the ``sandbox`` flag, visible to the LLM."""

    sandbox: bool = Field(
        default=True,
        description=(
            "沙箱开关。false 时执行环境清洗后的原始环境；"
            "主会话将请求人工审批，子代理/后台代理会被拒绝"
        ),
    )


class _ClassOrInstanceSchema:
    """Descriptor letting ``tool_call_schema`` be read from the class too.

    langchain_core defines ``tool_call_schema`` as a plain instance
    ``@property``, so class-level access (``SafeShellTool.tool_call_schema``)
    would return the bare property object instead of the schema model. The
    Task 6 acceptance one-liner reads the schema from the class; this
    descriptor forwards BOTH access forms to the inherited property getter
    (class access synthesizes a throwaway instance). The explicit
    ``args_schema`` mechanism is untouched.
    """

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        # Class access synthesizes a throwaway instance (cheap: all-defaults
        # pydantic model) so the inherited getter runs on a real self.
        owner = objtype if objtype is not None else type(obj)
        target = obj if obj is not None else owner()
        return ShellTool.tool_call_schema.__get__(target, type(target))


class SafeShellTool(ShellTool):
    """
    name: str = "terminal"
    description: str = "Run shell commands in a sandboxed workspace."
    """

    tool_call_schema: ClassVar[Any] = _ClassOrInstanceSchema()

    # ShellTool sets args_schema=ShellInput EXPLICITLY — signature-only changes
    # never propagate; the subclass must be wired here (plan line 559).
    args_schema: type[BaseModel] = SafeShellInput

    # Declared as a pydantic field so super().__init__(root_dir=...) is a real
    # kwarg (pre-Task-6 code passed it as an extra, which pydantic silently
    # dropped; declaring it makes the parameter real and fixes the
    # basedpyright reportCallIssue).
    root_dir: str | None = None

    def __init__(self, root_dir: str | None = None):
        # root_dir defaults to None so the ClassVar schema descriptor can
        # synthesize a throwaway instance for class-level schema access.
        # Stored via direct field assignment: super().__init__ is ShellTool's
        # synthesized pydantic __init__, which has no root_dir parameter
        # (pre-Task-6 code passed it as an extra kwarg that pydantic dropped).
        super().__init__()
        self.root_dir = root_dir
        # Detect system encoding (Windows typically uses GBK/codepage 936)
        self._encoding = locale.getpreferredencoding() or "utf-8"
        self.metadata = {"idempotent": False}

    # ── Guards & helpers ────────────────────────────────────────────────────

    @staticmethod
    def _join_commands(commands: str | list[str]) -> str:
        """Normalize to the historical command string (" && " join for lists)."""
        if isinstance(commands, list):
            return " && ".join(commands)
        return commands

    def _deny_sandbox_bypass(self, sandbox: bool) -> None:
        """Guard sandbox=False calls: subagents are denied, REQUIRED denies all.

        Matrix cells (agent/tools/pub_base/sandbox.py docstring): required +
        sandbox=False → DENIED outright; subagent scope + sandbox=False →
        DENIED (bypass is a main-session, human-approved decision only —
        the interrupt() wiring itself is Task 8). ``sandbox=True`` is never
        gated here.
        """
        if sandbox:
            return
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        scope = metadata.get("caller_scope", "main")
        if scope != "main":
            raise ToolException(
                f"沙箱绕过仅限主会话人工审批；当前 scope={scope}"
            )
        if read_policy() is SandboxPolicy.REQUIRED:
            raise ToolException(
                "SANDBOX_POLICY=required 拒绝未沙箱执行："
                "sandbox=False 需要主会话（main）人工审批"
            )

    @staticmethod
    def _check_dangerous(joined: str) -> None:
        """Raise on a dangerous command (regex over the joined string)."""
        if DANGEROUS_COMMAND_REGEX.search(joined):
            raise ToolException(_BLOCKED_MESSAGE)

    def _resolve_sandbox_argv(
        self, cmd_str: str, env: dict[str, str]
    ) -> tuple[list[str] | None, dict[str, str]]:
        """Resolve the sandboxed argv for ``cmd_str`` (list-exec form).

        Returns ``(None, env)`` when the shell fallback must be used: policy
        OFF (get_backend never consulted) or no usable backend (ONE loguru
        degrade warning line — the tool layer owns it, get_backend stays
        silent). REQUIRED + unavailable raises RuntimeError inside
        get_backend, wrapped here into a ToolException so
        handle_tool_error=True surfaces it as a tool error.
        """
        policy = read_policy()
        if policy is SandboxPolicy.OFF:
            return None, env
        try:
            backend = get_backend(policy)
        except RuntimeError as exc:
            raise ToolException(str(exc)) from exc
        if backend is None:
            logger.warning(
                "terminal: sandbox requested but no backend available "
                f"(policy={policy.value}) — degrading to unsandboxed shell execution"
            )
            return None, env
        # List-exec form for the sandbox: ["/bin/sh", "-c", cmd] is
        # semantically identical to POSIX shell=True (and Windows never has a
        # backend, so cmd.exe is untouched).
        return backend.wrap(["/bin/sh", "-c", cmd_str], env)

    # ── Sync execution paths ────────────────────────────────────────────────

    def _execute_sync(
        self,
        argv: str | list[str],
        *,
        shell: bool,
        env: dict[str, str] | None,
        encoding: str,
    ) -> str:
        """Single sync spawn point: Popen with explicit encoding + timeout.

        ``shell=True`` only for the string fallback path; the sandboxed path
        execs a list (no shell kwarg at all).
        """
        proc: subprocess.Popen[bytes] | None = None
        try:
            if shell:
                proc = subprocess.Popen(
                    argv,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=str(ROOT_DIR),
                    env=env,
                )
            else:
                proc = subprocess.Popen(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=str(ROOT_DIR),
                    env=env,
                )
            stdout_bytes, _ = proc.communicate(timeout=TERMINAL_TIMEOUT)
            output = stdout_bytes.decode(encoding, errors="replace")
            if proc.returncode != 0:
                return f"Exit code {proc.returncode}\n{output}"
            return output
        except subprocess.TimeoutExpired:
            if proc:
                proc.kill()
                proc.communicate()
            logger.warning(
                "terminal command timed out after {}s: {}",
                TERMINAL_TIMEOUT,
                str(argv)[:120],
            )
            return (
                f"Terminal command timed out after {TERMINAL_TIMEOUT} seconds. "
                "The command was forcibly terminated. Please try a simpler command."
            )
        except Exception as e:
            return f"Error: {e}"

    def _run_with_encoding(
        self,
        commands: str | list[str],
        encoding: str,
        env: dict[str, str] | None = None,
    ) -> str:
        """Run command with explicit encoding for stdout/stderr, with timeout.

        BYTE-IDENTICAL Windows fallback (SANDBOX_PLAN.md:546): the command
        string construction (str join / passthrough) and ``shell=True`` are
        unchanged vs pre-Task-6; the ONLY addition is ``env=``.
        """
        cmd_str = self._join_commands(commands)
        return self._execute_sync(cmd_str, shell=True, env=env, encoding=encoding)

    def _run_wrapped(self, argv: list[str], env: dict[str, str]) -> str:
        """Sandboxed sync path: list-exec of the backend-wrapped argv."""
        return self._execute_sync(
            argv, shell=False, env=env, encoding=self._encoding
        )

    # ── Tool entry points ───────────────────────────────────────────────────

    @override
    def _run(
        self,
        commands: str | list[str],
        run_manager: CallbackManagerForToolRun | None = None,
        sandbox: bool = True,
        **kwargs: Any,
    ) -> str:
        cmd_str = self._join_commands(commands)
        self._deny_sandbox_bypass(sandbox)
        self._check_dangerous(cmd_str)

        env = scrub_env()
        if sandbox:
            argv, wrapped_env = self._resolve_sandbox_argv(cmd_str, env)
            if argv is not None:
                return self._run_wrapped(argv, wrapped_env)

        # ShellTool._run() delegates to BashProcess which uses subprocess.run(check=True)
        # without timeout — prone to hanging and fails on Windows for console-dependent
        # commands (e.g. `timeout` needs a real console handle). Bypass it entirely and
        # use _run_with_encoding which has proper timeout and encoding handling.
        return self._run_with_encoding(commands, encoding=self._encoding, env=env)

    @override
    async def _arun(
        self,
        commands: str | list[str],
        run_manager: AsyncCallbackManagerForToolRun | None = None,
        sandbox: bool = True,
        **kwargs: Any,
    ) -> str:
        """Async version: non-blocking subprocess via asyncio.

        Unlike the sync _run() which blocks the event loop with
        proc.communicate(timeout=...), this version uses
        asyncio.create_subprocess_shell/exec so the event loop can
        process cancellation signals (answering=False) while
        the command is running.
        """
        cmd_str = self._join_commands(commands)
        self._deny_sandbox_bypass(sandbox)
        self._check_dangerous(cmd_str)

        env = scrub_env()
        argv: list[str] | None = None
        spawn_env = env
        if sandbox:
            argv, spawn_env = self._resolve_sandbox_argv(cmd_str, env)

        proc = None
        try:
            if argv is not None:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=spawn_env,
                    cwd=str(ROOT_DIR),
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    cmd_str,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(ROOT_DIR),
                    env=env,
                )
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=TERMINAL_TIMEOUT
            )
            output = stdout_bytes.decode(self._encoding, errors="replace")
            if proc.returncode != 0:
                return f"Exit code {proc.returncode}\n{output}"
            return output
        except asyncio.TimeoutError:
            if proc:
                proc.kill()
                await proc.communicate()
            logger.warning(
                "terminal command timed out after {}s: {}", TERMINAL_TIMEOUT, cmd_str[:120]
            )
            return (
                f"Terminal command timed out after {TERMINAL_TIMEOUT} seconds. "
                "The command was forcibly terminated. Please try a simpler command."
            )
        except asyncio.CancelledError:
            if proc:
                proc.kill()
            logger.warning("terminal command cancelled: {}", cmd_str[:120])
            return "Terminal command was cancelled."
        except Exception as e:
            return f"Error: {e}"


def build_terminal_tool() -> SafeShellTool:
    tool = SafeShellTool(root_dir=str(ROOT_DIR))
    tool.handle_tool_error = True
    return tool
