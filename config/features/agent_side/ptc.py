"""Programmatic Tool Calling (PTC) configuration.

PTC exposes an ``execute_code`` tool to a single functional role — the
EXECUTOR subagent — so it can run a Python script in an isolated child
process and call Sherry tools synchronously over a localhost TCP RPC bridge.
See ``agent/tools/ptc/`` and ``wiki/subagent/README.md`` for the design.

Tool names in ``ptc_allowed_tools`` are the *real* names emitted by the tool
registry (``patch_file``, not ``patch``). The effective set at runtime is the
intersection with the tools actually available to the child, so listing a
tool the EXECUTOR role does not carry is harmless; the intersection is what
is enforced.
"""

from typing import TypedDict


class PtcConfig(TypedDict):
    """Configuration for the Programmatic Tool Calling subsystem."""

    ptc_timeout_seconds: int
    ptc_max_tool_calls: int
    ptc_max_stdout_bytes: int
    ptc_max_stderr_bytes: int
    ptc_allowed_tools: list[str]
    #: Observation gate for the replay path: skill review compiles a per-step
    #: script, and an executor step subagent may prefill it before React.
    #: Shipped OFF: enable only after the observation pass (compile hit rate,
    #: prefill success rate, duplicate-side-effect audit).
    ptc_prefill_enabled: bool
    #: Hard cap for a generated script read from a skill's scripts/ directory.
    ptc_script_max_bytes: int
    #: Compiler attempts per step (JSON/syntax misses only; never executes).
    ptc_compile_max_attempts: int


PTC: PtcConfig = {
    "ptc_timeout_seconds": 120,
    "ptc_max_tool_calls": 50,
    "ptc_max_stdout_bytes": 50_000,
    "ptc_max_stderr_bytes": 10_000,
    # Safe tools only. Never includes execute_code itself (anti-recursion) nor
    # spawn/yield/kill/steer/memory/skill/question.
    "ptc_allowed_tools": [
        "read_file",
        "write_file",
        "patch_file",
        "terminal",
        "search_files",
        "web_search",
    ],
    "ptc_prefill_enabled": False,
    "ptc_script_max_bytes": 20_480,
    "ptc_compile_max_attempts": 2,
}
