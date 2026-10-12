"""Terminal tool with sandbox, blacklist, timeout, and env scrubbing.

Sandbox-hardening additions (see docs/sandbox/README.md):

- ``SafeShellInput`` subclass: the ``sandbox`` flag is exposed to the LLM via
  an explicit ``args_schema`` override (ShellTool declares args_schema
  explicitly, so signature-only changes never propagate — the schema must be
  subclassed). The ClassVar ``tool_call_schema`` is assigned through the
  shared factory ``class_or_instance_schema(ShellTool)`` (pub_base/
  schema_utils.py), so it can be read from the class too (langchain_core
  1.4.7 defines it as a bare instance ``@property``).
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

import hashlib
import locale
import asyncio
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, ClassVar
from loguru import logger
from pydantic import BaseModel, Field
from typing import override
from config import ROOT_DIR
from config.features import TOOLS_TIMEOUTS
from langchain_community.tools import ShellTool
from langchain_community.tools.shell.tool import ShellInput
from langchain_core.callbacks import CallbackManagerForToolRun, AsyncCallbackManagerForToolRun
from langchain_core.tools import ToolException

from agent.tools.pub_base import (
    SessionId,
    _extract_session_id,
    resolve_tool_session_id,
)

from agent.tools.pub_base.atomic_write import sweep_stale_temp_files
from agent.tools.pub_base.env_scrub import scrub_env
from agent.tools.pub_base.process_reap import areap_process, reap_process
from pub.func.message.eviction import build_file_preview, get_eviction_dir
from agent.security.terminal_output import strip_control_sequences
from agent.tools.pub_base.sandbox import SandboxPolicy, get_backend, read_policy
from agent.tools.pub_base.sandbox_guard import SandboxGuardMixin
from agent.tools.pub_base.schema_utils import class_or_instance_schema
from agent.tools.todolist.evidence_recorder import record_verification_evidence

#: The calling session travels in the graph state (the same channel the file
#: tools use). The runnable-config lookup in ``_extract_session_id`` is empty in
#: production, so a tool that relied on it alone resolved against the process
#: root instead of the session's project directory (found by the live smoke).

# Bound to the feature registry (single source of truth); name preserved.
TERMINAL_TIMEOUT = TOOLS_TIMEOUTS["terminal_timeout_seconds"]

#: Terminal stdout at or above this many bytes never rides a pipe into the
#: Python process: the child writes straight to a temp file and only a head/tail
#: preview comes back (the file itself moves into the session's evicted/ dir).
#: Below it the bytes are read back whole — the historical behaviour, and the
#: only sane one for the small outputs nearly every command produces. 0 (or
#: less) disables eviction entirely: every output is read back in memory.
FD_WRITE_THRESHOLD = TOOLS_TIMEOUTS["fd_write_threshold_bytes"]

#: Prefix for the fd-direct-write temp file. The sweep recognises this prefix
#: (``.log`` suffix) and removes abandoned leftovers, so a hard kill cannot
#: accumulate them (see ``agent/tools/pub_base/atomic_write.py``).
_STDOUT_TMP_PREFIX = ".sherry-tmp-stdout-"
_STDOUT_TMP_SUFFIX = ".log"
#: Bytes hashed for the evicted file's name — hashing the whole payload would
#: read it all back, which is exactly what the fd path exists to avoid.
_EVICT_NAME_HASH_BYTES = 65_536

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


def _open_stdout_tmp(session_id: str) -> tuple[Path, Any]:
    """Create the fd-direct-write stdout file; return ``(path, handle)``.

    The file lives in the SESSION directory whenever the id is a safe path
    segment, so the later ``rename`` into ``evicted/`` is a same-filesystem
    move; an unsafe/empty id falls back to the OS temp directory (where the
    move degrades to a streamed copy). Abandoned leftovers from a hard kill are
    swept here — the same prefix contract the atomic writes use.
    """
    from config import SESSIONS_DIR
    from config.path import is_safe_session_segment

    directory: str | None = None
    if session_id and is_safe_session_segment(session_id):
        candidate = Path(SESSIONS_DIR) / session_id
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            sweep_stale_temp_files(candidate)
            directory = str(candidate)
        except OSError:
            directory = None
    fd, name = tempfile.mkstemp(prefix=_STDOUT_TMP_PREFIX, suffix=_STDOUT_TMP_SUFFIX, dir=directory)
    return Path(name), os.fdopen(fd, "wb")


def _read_or_evict_output(tmp_path: Path, session_id: str, encoding: str) -> str:
    """Return the command's stdout: read whole under the threshold, else preview.

    Over the threshold the file is MOVED into the session's ``evicted/``
    directory (a same-filesystem rename when the temp file was created there)
    and only a head/tail preview is returned — the payload never rides the
    Python heap. Every failure path degrades to reading the file, because
    losing a command's output is worse than one oversized read.
    """
    try:
        size = tmp_path.stat().st_size
    except OSError:
        return ""
    if FD_WRITE_THRESHOLD <= 0 or size < FD_WRITE_THRESHOLD:
        data = b""
        try:
            data = tmp_path.read_bytes()
        except OSError:
            logger.debug("terminal: stdout temp file read failed", exc_info=True)
        finally:
            tmp_path.unlink(missing_ok=True)
        return data.decode(encoding, errors="replace")

    evict_dir = get_eviction_dir(session_id) if session_id else None
    if evict_dir is not None:
        try:
            with tmp_path.open("rb") as handle:
                digest = hashlib.sha256(handle.read(_EVICT_NAME_HASH_BYTES)).hexdigest()[:16]
            dest = evict_dir / f"terminal-{digest}.log"
            try:
                tmp_path.rename(dest)
            except OSError:
                # Cross-filesystem (OS temp dir fallback): stream, never read whole.
                shutil.copyfile(tmp_path, dest)
                tmp_path.unlink(missing_ok=True)
            logger.debug(
                "terminal: stdout {} bytes > {} — evicted to {}",
                size,
                FD_WRITE_THRESHOLD,
                dest,
            )
            return build_file_preview(dest, dest)
        except OSError:
            logger.warning("terminal: stdout eviction failed; reading it back", exc_info=True)

    data = b""
    try:
        data = tmp_path.read_bytes()
    except OSError:
        logger.debug("terminal: stdout temp file read failed", exc_info=True)
    finally:
        tmp_path.unlink(missing_ok=True)
    return data.decode(encoding, errors="replace")


def _discard_stdout_tmp(handle: Any, path: Path | None) -> None:
    """Close + unlink the fd stdout file on a failure path (never raises)."""
    if handle is not None:
        try:
            handle.close()
        except OSError:
            pass
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


class SafeShellInput(ShellInput):
    """ShellInput + the ``sandbox`` flag, visible to the LLM."""

    session_id: SessionId = ""

    sandbox: bool = Field(
        default=True,
        description=(
            "Sandbox toggle. When false, runs in the raw env-scrubbed environment; "
            "the main session will request human approval, while subagents/background "
            "agents are denied"
        ),
    )


class SafeShellTool(SandboxGuardMixin, ShellTool):
    """
    name: str = "terminal"
    description: str = "Run shell commands in a sandboxed workspace."
    """

    tool_call_schema: ClassVar[Any] = class_or_instance_schema(ShellTool)

    # ShellTool sets args_schema=ShellInput EXPLICITLY — signature-only changes
    # never propagate; the subclass must be wired here.
    args_schema: type[BaseModel] = SafeShellInput

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
        self.metadata = {"idempotent": False}

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
            tmp_path, tmp_handle = _open_stdout_tmp(session_id)
            if shell:
                # The terminal tool exists to run shell commands: this is the
                # unsandboxed fallback path (SANDBOX_POLICY=off or no OS
                # backend), and every call is gated by the HITL approval /
                # dangerous-command policy upstream of this spawn point.
                proc = subprocess.Popen(
                    argv,
                    shell=True,  # nosec B602
                    stdout=tmp_handle,
                    stderr=subprocess.STDOUT,
                    cwd=cwd,
                    env=env,
                )
            else:
                proc = subprocess.Popen(
                    argv,
                    stdout=tmp_handle,
                    stderr=subprocess.STDOUT,
                    cwd=cwd or str(ROOT_DIR),
                    env=env,
                )
            # The child holds its own descriptor; drop ours so the file is
            # complete the moment the child exits.
            tmp_handle.close()
            tmp_handle = None
            proc.wait(timeout=TERMINAL_TIMEOUT)
            # A TUI/progress command emits ANSI escapes and cursor reports
            # that say nothing to a reader but cost context (and carry an
            # injection surface of their own).
            output = strip_control_sequences(_read_or_evict_output(tmp_path, session_id, encoding))
            tmp_path = None  # read or evicted: nothing left to clean
            if proc.returncode != 0:
                return f"Exit code {proc.returncode}\n{output}"
            return output
        except subprocess.TimeoutExpired:
            # Bounded cleanup: kill, then wait with a deadline.
            if proc:
                reap_process(proc)
            _discard_stdout_tmp(tmp_handle, tmp_path)
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
            _discard_stdout_tmp(tmp_handle, tmp_path)
            logger.debug("terminal command failed: {!r}", argv[:1], exc_info=True)
            return f"Error: {e}"

    def _run_with_encoding(
        self,
        commands: str | list[str],
        encoding: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        session_id: str = "",
    ) -> str:
        """Run command with explicit encoding for stdout/stderr, with timeout.

        BYTE-IDENTICAL Windows fallback: the command string construction
        (str join / passthrough) and ``shell=True`` are unchanged vs the
        pre-sandbox-hardening path; the ONLY addition is ``env=``.
        """
        cmd_str = self._join_commands(commands)
        return self._execute_sync(
            cmd_str, shell=True, env=env, encoding=encoding, cwd=cwd, session_id=session_id
        )

    def _run_wrapped(
        self, argv: list[str], env: dict[str, str], cwd: str, session_id: str = ""
    ) -> str:
        """Sandboxed sync path: list-exec of the backend-wrapped argv."""
        return self._execute_sync(
            argv, shell=False, env=env, encoding=self._encoding, cwd=cwd, session_id=session_id
        )

    # ── Tool entry points ───────────────────────────────────────────────────

    @override
    def _run(
        self,
        commands: str | list[str],
        run_manager: CallbackManagerForToolRun | None = None,
        sandbox: bool = True,
        session_id: str = "",
        **kwargs: Any,
    ) -> str:
        cmd_str = self._join_commands(commands)
        self._deny_sandbox_bypass(sandbox)
        self._check_dangerous(cmd_str)
        self._check_sensitive_file_access(cmd_str)
        bound_session = resolve_tool_session_id(session_id, run_manager)
        cwd = self._resolve_cwd(bound_session)
        writable_roots = self._session_writable_roots(bound_session)

        env = scrub_env()
        if sandbox:
            argv, wrapped_env = self._resolve_sandbox_argv(cmd_str, env, writable_roots)
            if argv is not None:
                return self._record_verification(
                    cmd_str,
                    self._run_wrapped(argv, wrapped_env, cwd, session_id=bound_session),
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
            ),
            run_manager,
            sandboxed=False,  # no backend (or sandbox=False): a bare run
        )

    @override
    async def _arun(
        self,
        commands: str | list[str],
        run_manager: AsyncCallbackManagerForToolRun | None = None,
        sandbox: bool = True,
        session_id: str = "",
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
        self._check_sensitive_file_access(cmd_str)
        bound_session = resolve_tool_session_id(session_id, run_manager)
        cwd = self._resolve_cwd(bound_session)
        writable_roots = self._session_writable_roots(bound_session)

        env = scrub_env()
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
            tmp_path, tmp_handle = _open_stdout_tmp(bound_session)
            if argv is not None:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=tmp_handle,
                    stderr=asyncio.subprocess.STDOUT,
                    env=spawn_env,
                    cwd=cwd,
                )
                ran_sandboxed = True
            else:
                proc = await asyncio.create_subprocess_shell(
                    cmd_str,
                    stdout=tmp_handle,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=cwd or str(ROOT_DIR),
                    env=env,
                )
            tmp_handle.close()
            tmp_handle = None
            await asyncio.wait_for(proc.wait(), timeout=TERMINAL_TIMEOUT)
            output = strip_control_sequences(
                _read_or_evict_output(tmp_path, bound_session, self._encoding)
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
                await areap_process(proc)
            _discard_stdout_tmp(tmp_handle, tmp_path)
            logger.warning(
                "terminal command timed out after {}s: {}", TERMINAL_TIMEOUT, cmd_str[:120]
            )
            return self._record_verification(
                cmd_str,
                (
                    f"Terminal command timed out after {TERMINAL_TIMEOUT} seconds. "
                    "The command was forcibly terminated. Please try a simpler command."
                ),
                run_manager,
            )
        except asyncio.CancelledError:
            if proc:
                proc.kill()
            _discard_stdout_tmp(tmp_handle, tmp_path)
            logger.warning("terminal command cancelled: {}", cmd_str[:120])
            return self._record_verification(
                cmd_str, "Terminal command was cancelled.", run_manager
            )
        except Exception as e:
            _discard_stdout_tmp(tmp_handle, tmp_path)
            logger.debug("terminal: command dispatch failed", exc_info=True)
            return self._record_verification(cmd_str, f"Error: {e}", run_manager)


def build_terminal_tool() -> SafeShellTool:
    tool = SafeShellTool(root_dir=str(ROOT_DIR))
    tool.handle_tool_error = True
    return tool
