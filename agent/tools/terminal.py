"""Terminal tool with sandbox and blacklist."""

import locale
import subprocess
from config import ROOT_DIR
from langchain_community.tools import ShellTool


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


    def _run(self, commands: str | list[str], **kwargs) -> str:
        for bad in BLACKLIST:
            if bad in commands:
                return "Blocked: unsafe command."

        try:
            return super()._run(commands, **kwargs)
        except UnicodeDecodeError:
            # Fallback: retry with system encoding (GBK on Chinese Windows)
            return self._run_with_encoding(commands, encoding=self._encoding)

    def _run_with_encoding(self, commands: str | list[str], encoding: str) -> str:
        """Run command with explicit encoding for stdout/stderr."""
        if isinstance(commands, list):
            cmd_str = " && ".join(commands)
        else:
            cmd_str = commands

        try:
            proc = subprocess.Popen(
                cmd_str,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(ROOT_DIR),
            )
            stdout_bytes, _ = proc.communicate()
            output = stdout_bytes.decode(encoding, errors="replace")
            if proc.returncode != 0:
                return f"Exit code {proc.returncode}\n{output}"
            return output
        except Exception as e:
            return f"Error: {e}"


def build_terminal_tool() -> SafeShellTool:
    tool = SafeShellTool(root_dir = str(ROOT_DIR))
    tool.handle_tool_error = True
    return tool

