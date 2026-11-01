"""Terminal tools: the shell tool and the background-task surface."""

from .bg_registry import (
    BackgroundTask as BackgroundTask,
    BackgroundTaskRegistry as BackgroundTaskRegistry,
    background_registry as background_registry,
)
from .bg_tools import (
    build_terminal_bg_tool as build_terminal_bg_tool,
    terminal_bg as terminal_bg,
    terminal_bg_list as terminal_bg_list,
    terminal_bg_output as terminal_bg_output,
)
from .core import (
    DANGEROUS_COMMAND_REGEX as DANGEROUS_COMMAND_REGEX,
    SafeShellInput as SafeShellInput,
    SafeShellTool as SafeShellTool,
    TERMINAL_MAX_TIMEOUT as TERMINAL_MAX_TIMEOUT,
    TERMINAL_TIMEOUT as TERMINAL_TIMEOUT,
    _record_shell_writes as _record_shell_writes,
    build_terminal_tool as build_terminal_tool,
)

__all__ = [
    "BackgroundTask",
    "BackgroundTaskRegistry",
    "DANGEROUS_COMMAND_REGEX",
    "SafeShellInput",
    "SafeShellTool",
    "TERMINAL_MAX_TIMEOUT",
    "TERMINAL_TIMEOUT",
    "background_registry",
    "build_terminal_bg_tool",
    "build_terminal_tool",
    "terminal_bg",
    "terminal_bg_list",
    "terminal_bg_output",
]
