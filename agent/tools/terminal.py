"""Terminal tool with sandbox, blacklist, timeout, and env scrubbing.

Sandbox-hardening additions (see wiki/sandbox/README.md):

- ``SafeShellInput``: the LLM-facing schema (``commands`` + the ``sandbox`` /
  ``run_in_background`` / ``timeout`` additions), wired as ``args_schema`` so
  the model sees exactly this surface (frozen by
  ``tests/agent/tools/test_tool_schema_goldens.py``).
- ``sandbox`` parameter on ``_run``/``_arun`` (langchain injects schema fields
  by name), with scope/policy guards: ``caller_scope != "main"`` +
  ``sandbox=False`` is denied outright; ``SANDBOX_POLICY=required`` +
  ``sandbox=False`` is denied (main-session human approval wiring arrives
  with — no ``interrupt()`` here).
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
- fd-direct-write stdout: BOTH spawn sites point the child's stdout at a FILE
  (``.sherry-tmp-stdout-*.log`` in the session dir, stderr merged into it) and
  wait on the process instead of draining a pipe, so an oversized output never
  sits on the Python heap. Under ``TOOLS_TIMEOUTS["fd_write_threshold_bytes"]``
  the file is read whole and removed; at or above it the file moves into the
  session's ``evicted/`` directory and only a byte-bounded head/tail preview
  comes back (see ``pub/func/message/eviction.py::build_file_preview``).
"""

from __future__ import annotations

import locale
import asyncio
import os
from pathlib import Path
import re
import subprocess
import warnings
from typing import Any, ClassVar
from loguru import logger
from pydantic import BaseModel, Field, model_validator
from typing import override
from config import ROOT_DIR
from config.features import TERMINAL_BG, TOOLS_TIMEOUTS
from langchain_core.tools import BaseTool
from langchain_core.callbacks import CallbackManagerForToolRun, AsyncCallbackManagerForToolRun
from langchain_core.tools import ToolException

from agent.tools.pub_base import (
    SessionId,
    _extract_session_id,
    resolve_tool_session_id,
)
from agent.tools.pub_base.shadow_git import ensure_checkpoint


from agent.tools.pub_base.env_scrub import scrub_env
from agent.tools.pub_base.process_reap import akill_process_tree, kill_process_tree
from agent.tools.pub_base.shell_resolver import resolve_windows_bash
from agent.tools.pub_base.stdout_fd import (
    discard_stdout_tmp,
    open_stdout_tmp,
    read_or_evict_output,
)
from agent.security.terminal_output import strip_control_sequences
from agent.tools.pub_base.sandbox import SandboxPolicy, get_backend, read_policy
from agent.tools.pub_base.sandbox_guard import SandboxGuardMixin
from agent.tools.pub_base.schema_utils import class_or_instance_schema
from agent.tools.pub_base.evidence_recorder import record_verification_evidence

#: The calling session travels in the graph state (the same channel the file
#: tools use). The runnable-config lookup in ``_extract_session_id`` is empty in
#: production, so a tool that relied on it alone resolved against the process
#: root instead of the session's project directory (found by the live smoke).

# Bound to the feature registry (single source of truth); name preserved.
TERMINAL_TIMEOUT = TOOLS_TIMEOUTS["terminal_timeout_seconds"]
#: Ceiling for a per-call ``timeout`` (see ``_resolve_timeout``).
TERMINAL_MAX_TIMEOUT = TOOLS_TIMEOUTS["terminal_max_timeout_seconds"]


def _process_group_kwargs() -> dict[str, Any]:
    """Make the child a process-group leader so a tree kill cannot escape.

    POSIX uses ``start_new_session`` (the PID becomes the PGID); Windows uses a
    new process group, which ``taskkill /T`` walks. The background path also
    records the PGID for the eventual session-cleanup kill.
    """
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _resolve_timeout(requested: int | None) -> int:
    """The effective per-call timeout: defaulted, clamped, never nonsense.

    ``None``/0/negative falls back to ``TERMINAL_TIMEOUT`` (the historical 30 s);
    anything larger is clamped to ``TERMINAL_MAX_TIMEOUT`` so a bad number can
    never park a turn for hours.
    """
    if requested is None or requested <= 0:
        return TERMINAL_TIMEOUT
    return min(int(requested), TERMINAL_MAX_TIMEOUT)


# Raised as a ToolException instead of returned, so handle_tool_error=True
# routes it through the error ToolMessage channel.
_BLOCKED_MESSAGE = "Blocked: unsafe command."

# Sensitive-file access refusal: the model must go through read_file /
# external-path approval instead of reading credentials via shell.
_SENSITIVE_FILE_MESSAGE = (
    "Blocked: sensitive file access. Use the read_file tool so external paths go "
    "through the human approval gate instead of reading credentials or system "
    "files from the shell."
)

# Regex blacklist over the " && "-joined command string.
# Supersedes the old element-exact BLACKLIST set: "rm -rf /", "mkfs",
# "shutdown", "reboot" are all covered, plus the joined/chained variants the
# exact matcher missed (["echo ok", "rm -rf /"] was the defect).
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

# Sensitive-file regex list, matched case-insensitively against the
# joined command string after DANGEROUS_COMMAND_REGEX and before any spawn.
# This is a MITIGATION, not a barrier: renaming the reader (`dd`, `sed`,
# `python -c "open(...)"`, `$(< file)`), a variable, a shell glob, or a
# heredoc all bypass a literal regex. The real read barrier is the OS sandbox
# read-shield in sandbox_bwrap.py / sandbox_seatbelt.py.
_SENSITIVE_FILE_PATTERNS = [
    re.compile(
        r"\b(?:cat|head|tail|less|more)\s+.*?(/etc/(?:passwd|shadow|sudoers))\b", re.IGNORECASE
    ),
    re.compile(r"\b(?:cat|head|tail)\s+.*?\.env\b", re.IGNORECASE),
    re.compile(r"\bcp\s+.*?\.ssh/", re.IGNORECASE),
    re.compile(r"\bcurl\s+.*?-d\s+@.*?\.env\b", re.IGNORECASE),
    re.compile(r"\b(?:cat|head|tail)\s+.*?~/.ssh/", re.IGNORECASE),
    re.compile(r"\b(?:cat|head|tail)\s+.*?~/.aws/", re.IGNORECASE),
]


class SafeShellInput(BaseModel):
    """The LLM-facing terminal schema.

    ``commands`` reproduces ``langchain_community``'s ``ShellInput`` verbatim —
    the field text and the ``str -> [str]`` before-validator, which the HITL
    layer relies on reading ``args.get("commands")`` — because the model sees
    this schema and that package is archived.
    ``tests/agent/tools/test_tool_schema_goldens.py`` freezes the surface.

    The validator keeps upstream's warning text on purpose: three test modules
    filter on it by that exact string, and the migration's bar is an unchanged
    surface.
    """

    commands: str | list[str] = Field(
        ..., description="List of shell commands to run. Deserialized using json.loads"
    )

    #: Runtime-injected; excluded from the model-facing schema.
    session_id: SessionId = ""

    @model_validator(mode="before")
    @classmethod
    def _validate_commands(cls, values: Any) -> Any:
        """Wrap a bare string into a one-element list (upstream semantics)."""
        if isinstance(values, dict):
            commands = values.get("commands")
            if not isinstance(commands, list):
                values["commands"] = [commands]
            warnings.warn("The shell tool has no safeguards by default. Use at your own risk.")
        return values

    sandbox: bool = Field(
        default=True,
        description=(
            "Sandbox toggle. When false, runs in the raw env-scrubbed environment; "
            "the main session will request human approval, while subagents/background "
            "agents are denied"
        ),
    )
    run_in_background: bool = Field(
        default=False,
        description=(
            "Run the command in the background and return immediately with a task id "
            "(the process keeps running; its output is captured to a file). Use "
            "terminal_bg_list to see this session's tasks and terminal_bg_output to "
            "read one's output. You are notified when it exits. Good for servers, "
            "watchers, and anything longer than the timeout."
        ),
    )
    timeout: int | None = Field(
        default=None,
        description=(
            "Optional max execution time in seconds. "
            f"Defaults to {TERMINAL_TIMEOUT}s, max {TERMINAL_MAX_TIMEOUT}s. "
            "Use a longer value for compilation, installation, or long builds."
        ),
    )


class SafeShellTool(SandboxGuardMixin, BaseTool):
    """Run shell commands (sandbox-wrapped, session-scoped cwd).

    Re-based on ``BaseTool``: the former ``ShellTool`` parent came from the
    archived ``langchain-community`` and contributed nothing this tool did not
    already replace (its ``_run`` delegated to a ``BashProcess``, its ``process``
    / ``ask_human_input`` fields went unused). The model-facing surface is
    unchanged — pinned by ``tests/agent/tools/test_tool_schema_goldens.py``.
    """

    name: str = "terminal"
    # The DESCRIPTION deliberately stays upstream's sentence ("Run shell commands
    # on this Linux machine."): it is what the model has always been shown, and
    # the migration must not change the tool surface.
    description: str = "Run shell commands on this Linux machine."

    args_schema: type[BaseModel] = SafeShellInput

    #: CLASS-level ``SafeShellTool.tool_call_schema`` must keep working:
    #: langchain-core defines it as a bare instance ``@property``, so the
    #: descriptor synthesizes a throwaway instance and forwards to ``BaseTool``'s
    #: getter (which derives the schema from ``args_schema`` above).
    tool_call_schema: ClassVar[Any] = class_or_instance_schema(BaseTool)

    # Legacy fallback working directory. Commands now resolve their cwd from the
    # session (``_resolve_cwd``); this field is only consulted when no session is
    # in scope at all (e.g. a direct ``_run`` without state), where it defaults
    # to the repository root — the pre-project-binding behaviour.
    root_dir: str | None = None

    def __init__(self, root_dir: str | None = None):
        # root_dir defaults to None so the ClassVar schema descriptor can
        # synthesize a throwaway instance for class-level schema access.
        # Stored via direct field assignment: super.__init__ is ShellTool's
        # synthesized pydantic __init__, which has no root_dir parameter — passing
        # one as an extra kwarg would be dropped silently.
        super().__init__()
        self.root_dir = root_dir
        # Detect system encoding (Windows typically uses GBK/codepage 936)
        self._encoding = locale.getpreferredencoding() or "utf-8"
        self.metadata = {"idempotent": False, "side_effect_scope": "system"}

    # ── Guards & helpers ────────────────────────────────────────────────────

    def _resolve_cwd(self, session_id: str | None) -> str:
        """The working directory for one command: the session's project dir.

        Read per call (never cached): the tools are process-level singletons and
        a session can switch directories at a turn boundary. A session without a
        binding falls back to the process default (env → sherry.jsonc → repo
        root), so an unbound session behaves exactly as before the feature.
        With no session in scope at all (a direct ``_run`` call), the
        constructor's ``root_dir`` applies — ``build_terminal_tool`` pins it to
        the repository root.
        """
        if not session_id:
            return self.root_dir or str(ROOT_DIR)
        from runtime.session.project_dir import current_project_dir

        return str(current_project_dir(session_id))

    @staticmethod
    def _join_commands(commands: str | list[str]) -> str:
        """Normalize to the historical command string (" && " join for lists)."""
        if isinstance(commands, list):
            return " && ".join(commands)
        return commands

    @staticmethod
    def _check_dangerous(joined: str) -> None:
        """Raise on a dangerous command (regex over the joined string)."""
        if DANGEROUS_COMMAND_REGEX.search(joined):
            raise ToolException(_BLOCKED_MESSAGE)

    @staticmethod
    def _check_sensitive_file_access(joined: str) -> None:
        """Raise when the command string tries to read a sensitive file.

        Runs after :meth:`_check_dangerous` and before any spawn. This is a
        mitigation layer (literal regex), not a read barrier — see the
        ``_SENSITIVE_FILE_PATTERNS`` comment for the known bypasses; the OS
        sandbox read-shield is the actual barrier.
        """
        for pattern in _SENSITIVE_FILE_PATTERNS:
            if pattern.search(joined):
                raise ToolException(_SENSITIVE_FILE_MESSAGE)

    @staticmethod
    def _record_verification(
        command: str, result: str, run_manager: Any, *, sandboxed: bool | None = None
    ) -> str:
        """Record verification evidence for a finished command, then return the result.

        ``sandboxed=False`` marks a bare run (no backend, or the caller's
        ``sandbox=False``); the ledger row then says so, which is the only
        durable trace an unsandboxed command leaves.
        """
        record_verification_evidence(
            command, result, _extract_session_id(run_manager), sandboxed=sandboxed
        )
        return result

    def _session_writable_roots(self, session_id: str | None) -> list[Path]:
        """The sandbox's write surface for this call: the session's project root.

        Same source as :meth:`_resolve_cwd`, so the command's cwd and its
        writable面 are always the same directory — a project bound outside the
        repository is writable (it used to be EROFS), and the repository is NOT
        writable for a session bound elsewhere (it used to be, which made
        ``terminal`` a way around the external-path gate).
        """
        cwd = self._resolve_cwd(session_id)
        return [Path(cwd)] if cwd else []

    def _resolve_sandbox_argv(
        self,
        cmd_str: str,
        env: dict[str, str],
        writable_roots: list[Path],
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
        return backend.wrap(["/bin/sh", "-c", cmd_str], env, writable_roots)

    # ── Sync execution paths ────────────────────────────────────────────────

    def _execute_sync(
        self,
        argv: str | list[str],
        *,
        shell: bool,
        env: dict[str, str] | None,
        encoding: str,
        cwd: str | None = None,
        session_id: str = "",
        timeout: int = TERMINAL_TIMEOUT,
    ) -> str:
        """Single sync spawn point: Popen with explicit encoding + timeout.

        ``shell=True`` only for the string fallback path; the sandboxed path
        execs a list (no shell kwarg at all).
        """
        proc: subprocess.Popen[bytes] | None = None
        tmp_path: Path | None = None
        tmp_handle: Any = None
        try:
            # stdout (stderr merged into it, exactly as before) goes to a FILE,
            # not a pipe: the kernel writes the child's output to disk, so this
            # process never holds the payload on its heap. A pipe also meant the
            # parent had to drain it to EOF — with a file that dependency is
            # gone, so a timeout can simply kill and reap.
            tmp_path, tmp_handle = open_stdout_tmp(session_id)
            if shell:
                # The terminal tool exists to run shell commands: this is the
                # unsandboxed fallback path (SANDBOX_POLICY=off or no OS
                # backend), and every call is gated by the HITL approval /
                # dangerous-command policy upstream of this spawn point.
                #
                # Git Bash first when it exists (Windows): the tool promises a
                # POSIX shell, and cmd.exe would quietly change what `&`, `&&`,
                # `$()` and `2>&1` mean. POSIX hosts get None here and keep the
                # platform shell.
                bash_exe = resolve_windows_bash()
                if bash_exe:
                    proc = subprocess.Popen(
                        [bash_exe, "-c", argv],  # argv is the joined command string
                        stdout=tmp_handle,
                        stderr=subprocess.STDOUT,
                        cwd=cwd,
                        env=env,
                        **_process_group_kwargs(),
                    )
                else:
                    proc = subprocess.Popen(
                        argv,
                        shell=True,  # nosec B602
                        stdout=tmp_handle,
                        stderr=subprocess.STDOUT,
                        cwd=cwd,
                        env=env,
                        **_process_group_kwargs(),
                    )
            else:
                proc = subprocess.Popen(
                    argv,
                    stdout=tmp_handle,
                    stderr=subprocess.STDOUT,
                    cwd=cwd or str(ROOT_DIR),
                    env=env,
                    **_process_group_kwargs(),
                )
            # The child holds its own descriptor; drop ours so the file is
            # complete the moment the child exits.
            tmp_handle.close()
            tmp_handle = None
            proc.wait(timeout=timeout)
            # A TUI/progress command emits ANSI escapes and cursor reports
            # that say nothing to a reader but cost context (and carry an
            # injection surface of their own).
            output = strip_control_sequences(
                read_or_evict_output(tmp_path, session_id, encoding, evict_prefix="terminal-")
            )
            tmp_path = None  # read or evicted: nothing left to clean
            if proc.returncode != 0:
                return f"Exit code {proc.returncode}\n{output}"
            return output
        except subprocess.TimeoutExpired:
            # Bounded cleanup: kill, then wait with a deadline.
            if proc:
                kill_process_tree(proc)
            discard_stdout_tmp(tmp_handle, tmp_path)
            logger.warning(
                "terminal command timed out after {}s: {}",
                timeout,
                str(argv)[:120],
            )
            return (
                f"Terminal command timed out after {timeout} seconds. "
                "The command was forcibly terminated. Please try a simpler command."
            )
        except Exception as e:
            discard_stdout_tmp(tmp_handle, tmp_path)
            logger.debug("terminal command failed: {!r}", argv[:1], exc_info=True)
            return f"Error: {e}"

    def _run_with_encoding(
        self,
        commands: str | list[str],
        encoding: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        session_id: str = "",
        timeout: int = TERMINAL_TIMEOUT,
    ) -> str:
        """Run command with explicit encoding for stdout/stderr, with timeout.

        BYTE-IDENTICAL Windows fallback: the command string construction
        (str join / passthrough) and ``shell=True`` are unchanged vs the
        pre-sandbox-hardening path; the ONLY addition is ``env=``.
        """
        cmd_str = self._join_commands(commands)
        return self._execute_sync(
            cmd_str,
            shell=True,
            env=env,
            encoding=encoding,
            cwd=cwd,
            session_id=session_id,
            timeout=timeout,
        )

    def _run_wrapped(
        self,
        argv: list[str],
        env: dict[str, str],
        cwd: str,
        session_id: str = "",
        timeout: int = TERMINAL_TIMEOUT,
    ) -> str:
        """Sandboxed sync path: list-exec of the backend-wrapped argv."""
        return self._execute_sync(
            argv,
            shell=False,
            env=env,
            encoding=self._encoding,
            cwd=cwd,
            session_id=session_id,
            timeout=timeout,
        )

    # ── Tool entry points ───────────────────────────────────────────────────

    @override
    def _run(
        self,
        commands: str | list[str],
        run_manager: CallbackManagerForToolRun | None = None,
        sandbox: bool = True,
        session_id: str = "",
        timeout: int | None = None,
        **kwargs: Any,
    ) -> str:
        cmd_str = self._join_commands(commands)
        effective_timeout = _resolve_timeout(timeout)
        self._deny_sandbox_bypass(sandbox)
        self._check_dangerous(cmd_str)
        self._check_sensitive_file_access(cmd_str)
        bound_session = resolve_tool_session_id(session_id, run_manager)
        cwd = self._resolve_cwd(bound_session)
        writable_roots = self._session_writable_roots(bound_session)
        # A command can write (`echo > file`, cp, mv) without the file tools;
        # take the turn's shadow snapshot before it runs. Fail-open.
        ensure_checkpoint(bound_session, reason=f"before_write:terminal:{cmd_str[:120]}")

        env = scrub_env()
        if sandbox:
            argv, wrapped_env = self._resolve_sandbox_argv(cmd_str, env, writable_roots)
            if argv is not None:
                return self._record_verification(
                    cmd_str,
                    self._run_wrapped(
                        argv, wrapped_env, cwd, session_id=bound_session, timeout=effective_timeout
                    ),
                    run_manager,
                    sandboxed=True,
                )

        # ShellTool._run delegates to BashProcess which uses subprocess.run(check=True)
        # without timeout — prone to hanging and fails on Windows for console-dependent
        # commands (e.g. `timeout` needs a real console handle). Bypass it entirely and
        # use _run_with_encoding which has proper timeout and encoding handling.
        return self._record_verification(
            cmd_str,
            self._run_with_encoding(
                commands,
                encoding=self._encoding,
                env=env,
                cwd=cwd,
                session_id=bound_session,
                timeout=effective_timeout,
            ),
            run_manager,
            sandboxed=False,  # no backend (or sandbox=False): a bare run
        )

    async def _run_background(
        self,
        cmd_str: str,
        session_id: str,
        cwd: str | None,
        env: dict[str, str],
        *,
        sandbox: bool,
        writable_roots: list[Path],
    ) -> str:
        """Spawn without waiting: register the task, return its id, notify on exit.

        The child's stdout goes to a RETAINED file (never a temp file, never a
        pipe) so the output can be read while it runs and after it exits; the
        watcher in ``terminal_bg_registry`` owns the exit notification. The
        session's live-task count is capped by ``TERMINAL_BG``.
        """
        import time as _time
        import uuid as _uuid

        from agent.tools.terminal_bg_registry import (
            BackgroundTask,
            background_registry,
            open_background_output,
        )

        if background_registry.running_count(session_id) >= TERMINAL_BG["max_tasks_per_session"]:
            running = [
                task.task_id
                for task in background_registry.list_session(session_id)
                if task.status == "running"
            ]
            return (
                f"Error: this session already has {len(running)} background task(s) running "
                f"(limit {TERMINAL_BG['max_tasks_per_session']}): {', '.join(running)}. "
                "Wait for one to finish (terminal_bg_list) or use the foreground path."
            )

        task_id = f"bg-{_uuid.uuid4().hex[:12]}"
        output_path = open_background_output(session_id, task_id)
        if output_path is None:
            return (
                "Error: could not create the output file for a background task (invalid session)."
            )

        task = BackgroundTask(
            task_id=task_id,
            session_id=session_id,
            pid=None,
            command=cmd_str,
            output_path=output_path,
            started_at=_time.monotonic(),
        )
        handle = output_path.open("ab")
        proc: asyncio.subprocess.Process | None = None
        try:
            # The same shell ladder as the foreground path: sandbox backend,
            # then Git Bash, then the platform shell.
            argv, spawn_env = (
                self._resolve_sandbox_argv(cmd_str, env, writable_roots) if sandbox else (None, env)
            )
            if argv is not None:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=handle,
                    stderr=asyncio.subprocess.STDOUT,
                    env=spawn_env,
                    cwd=cwd or str(ROOT_DIR),
                    **_process_group_kwargs(),
                )
            else:
                bash_exe = resolve_windows_bash()
                if bash_exe:
                    proc = await asyncio.create_subprocess_exec(
                        bash_exe,
                        "-c",
                        cmd_str,
                        stdout=handle,
                        stderr=asyncio.subprocess.STDOUT,
                        env=env,
                        cwd=cwd or str(ROOT_DIR),
                        **_process_group_kwargs(),
                    )
                else:
                    proc = await asyncio.create_subprocess_shell(
                        cmd_str,
                        stdout=handle,
                        stderr=asyncio.subprocess.STDOUT,
                        env=env,
                        cwd=cwd or str(ROOT_DIR),
                        **_process_group_kwargs(),
                    )
        except Exception as exc:  # noqa: BLE001 - a refused spawn must not leak the file
            from agent.tools.terminal_bg_registry import discard_background_output

            discard_background_output(task)
            logger.debug("terminal background spawn failed", exc_info=True)
            return f"Error: could not start the background command: {exc}"
        finally:
            handle.close()  # the child holds its own descriptor

        task.pid = proc.pid
        task.pgid = proc.pid  # start_new_session/creationflags made it the leader
        task.live = True
        background_registry.register(task)
        background_registry.start_watcher(task_id, proc, session_id)
        logger.info(
            "terminal: background task {} started (pid {}, session {}): {}",
            task_id,
            proc.pid,
            session_id,
            cmd_str[:120],
        )
        return (
            f"Background task started.\n"
            f"Task ID: {task_id}\n"
            f"PID: {proc.pid}\n"
            f"Output file: {output_path}\n"
            f"You will be notified when it exits. Use terminal_bg_list to check status "
            f'and terminal_bg_output(task_id="{task_id}") to read the output.'
        )

    @override
    async def _arun(
        self,
        commands: str | list[str],
        run_manager: AsyncCallbackManagerForToolRun | None = None,
        sandbox: bool = True,
        session_id: str = "",
        timeout: int | None = None,
        run_in_background: bool = False,
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
        effective_timeout = _resolve_timeout(timeout)
        self._deny_sandbox_bypass(sandbox)
        self._check_dangerous(cmd_str)
        self._check_sensitive_file_access(cmd_str)
        bound_session = resolve_tool_session_id(session_id, run_manager)
        cwd = self._resolve_cwd(bound_session)
        writable_roots = self._session_writable_roots(bound_session)
        # Same fail-open snapshot as the sync path.
        ensure_checkpoint(bound_session, reason=f"before_write:terminal:{cmd_str[:120]}")

        env = scrub_env()

        if run_in_background:
            return await self._run_background(
                cmd_str,
                bound_session,
                cwd,
                env,
                sandbox=sandbox,
                writable_roots=writable_roots,
            )

        argv: list[str] | None = None
        spawn_env = env
        ran_sandboxed = False
        if sandbox:
            argv, spawn_env = self._resolve_sandbox_argv(cmd_str, env, writable_roots)
            ran_sandboxed = argv is not None

        proc = None
        tmp_path: Path | None = None
        tmp_handle: Any = None
        try:
            # Same fd-direct-write contract as the sync path: stdout (merged
            # with stderr) lands in a file, so nothing oversized sits on the
            # event loop's heap and no pipe has to be drained to EOF.
            tmp_path, tmp_handle = open_stdout_tmp(bound_session)
            if argv is not None:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=tmp_handle,
                    stderr=asyncio.subprocess.STDOUT,
                    env=spawn_env,
                    cwd=cwd,
                    **_process_group_kwargs(),
                )
                ran_sandboxed = True
            else:
                # Same POSIX-shell preference as the sync path (Git Bash on
                # Windows when installed; the platform shell otherwise).
                bash_exe = resolve_windows_bash()
                if bash_exe:
                    proc = await asyncio.create_subprocess_exec(
                        bash_exe,
                        "-c",
                        cmd_str,
                        stdout=tmp_handle,
                        stderr=asyncio.subprocess.STDOUT,
                        cwd=cwd or str(ROOT_DIR),
                        env=env,
                        **_process_group_kwargs(),
                    )
                else:
                    proc = await asyncio.create_subprocess_shell(
                        cmd_str,
                        stdout=tmp_handle,
                        stderr=asyncio.subprocess.STDOUT,
                        cwd=cwd or str(ROOT_DIR),
                        env=env,
                        **_process_group_kwargs(),
                    )
            tmp_handle.close()
            tmp_handle = None
            await asyncio.wait_for(proc.wait(), timeout=effective_timeout)
            output = strip_control_sequences(
                read_or_evict_output(
                    tmp_path, bound_session, self._encoding, evict_prefix="terminal-"
                )
            )
            tmp_path = None  # read or evicted: nothing left to clean
            if proc.returncode != 0:
                return self._record_verification(
                    cmd_str,
                    f"Exit code {proc.returncode}\n{output}",
                    run_manager,
                    sandboxed=ran_sandboxed,
                )
            return self._record_verification(cmd_str, output, run_manager, sandboxed=ran_sandboxed)
        except TimeoutError:
            if proc:
                await akill_process_tree(proc)
            discard_stdout_tmp(tmp_handle, tmp_path)
            logger.warning(
                "terminal command timed out after {}s: {}", effective_timeout, cmd_str[:120]
            )
            return self._record_verification(
                cmd_str,
                (
                    f"Terminal command timed out after {timeout} seconds. "
                    "The command was forcibly terminated. Please try a simpler command."
                ),
                run_manager,
            )
        except asyncio.CancelledError:
            if proc:
                # Kill the whole foreground process group, not just the shell:
                # a pipeline/subshell would otherwise survive the stop.
                await akill_process_tree(proc, grace=1.0)
            discard_stdout_tmp(tmp_handle, tmp_path)
            logger.warning("terminal command cancelled: {}", cmd_str[:120])
            return self._record_verification(
                cmd_str, "Terminal command was cancelled (process tree killed).", run_manager
            )
        except Exception as e:
            discard_stdout_tmp(tmp_handle, tmp_path)
            logger.debug("terminal: command dispatch failed", exc_info=True)
            return self._record_verification(cmd_str, f"Error: {e}", run_manager)


def build_terminal_tool() -> SafeShellTool:
    tool = SafeShellTool(root_dir=str(ROOT_DIR))
    tool.handle_tool_error = True
    return tool
