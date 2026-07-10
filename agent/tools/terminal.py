"""Terminal tool with sandbox, blacklist, and timeout."""

import locale
import asyncio
import subprocess
from loguru import logger
from typing import override
from config import ROOT_DIR
from langchain_community.tools import ShellTool

TERMINAL_TIMEOUT = 30  # seconds
BLACKLIST = {"rm -rf /", "mkfs", "shutdown", "reboot"}


class SafeShellTool(ShellTool):
    """
        name: str = "terminal"
        description: str = "Run shell commands in a sandboxed workspace."
    """
    def __init__(self, root_dir):
        super().__init__(root_dir=root_dir)
        # Detect system encoding (Windows typically uses GBK/codepage 936)
        self._encoding = locale.getpreferredencoding() or "utf-8"
        self.metadata = {"idempotent": False}

    @override
    def _run(self, commands: str | list[str], **kwargs) -> str:
        for bad in BLACKLIST:
            if bad in commands:
                return "Blocked: unsafe command."

        # ShellTool._run() delegates to BashProcess which uses subprocess.run(check=True)
        # without timeout — prone to hanging and fails on Windows for console-dependent
        # commands (e.g. `timeout` needs a real console handle). Bypass it entirely and
        # use _run_with_encoding which has proper timeout and encoding handling.
        return self._run_with_encoding(commands, encoding=self._encoding)

    @override
    async def _arun(self, commands: str | list[str], **kwargs) -> str:
        """Async version: non-blocking subprocess via asyncio.

        Unlike the sync _run() which blocks the event loop with
        proc.communicate(timeout=...), this version uses
        asyncio.create_subprocess_shell so the event loop can
        process cancellation signals (answering=False) while
        the command is running.
        """
        for bad in BLACKLIST:
            if bad in commands:
                return "Blocked: unsafe command."

        if isinstance(commands, list):
            cmd_str = " && ".join(commands)
        else:
            cmd_str = commands

        proc = None
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd_str,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(ROOT_DIR),
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
            logger.warning("terminal command timed out after {}s: {}", TERMINAL_TIMEOUT, cmd_str[:120])
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

    def _run_with_encoding(self, commands: str | list[str], encoding: str) -> str:
        """Run command with explicit encoding for stdout/stderr, with timeout."""
        if isinstance(commands, list):
            cmd_str = " && ".join(commands)
        else:
            cmd_str = commands

        proc = None
        try:
            proc = subprocess.Popen(
                cmd_str,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(ROOT_DIR),
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
            logger.warning("terminal command timed out after {}s: {}", TERMINAL_TIMEOUT, cmd_str[:120])
            return (
                f"Terminal command timed out after {TERMINAL_TIMEOUT} seconds. "
                "The command was forcibly terminated. Please try a simpler command."
            )
        except Exception as e:
            return f"Error: {e}"


def build_terminal_tool() -> SafeShellTool:
    tool = SafeShellTool(root_dir = str(ROOT_DIR))
    tool.handle_tool_error = True
    return tool

