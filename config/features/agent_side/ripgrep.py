"""ripgrep backend for the file-search tool (resolver + spawn bounds)."""

from typing import TypedDict


class RipgrepConfig(TypedDict):
    """ripgrep-backed keyword search: discovery and how long a child may live.

    ``enabled`` is the rollback switch: with it False the search tool uses the
    pure-Python walk only, which is exactly the behaviour before ripgrep was
    wired in. The resolver falls back to that same walk whenever ``rg`` cannot
    be found or its ``--version`` probe fails, so disabling this is never
    required for correctness — only for pinning the old path deliberately.
    """

    enabled: bool
    # Discovery mirrors the ast-grep resolver (agent/tools/code_intel/ast_grep/
    # resolver.py) and adds one tier the project dependency needs:
    #   env override -> next to the running interpreter (.venv/bin, where
    #   `ripgrep-bin` puts its console script) -> provisioned runtime ->
    #   in-repo bin cache -> PATH -> Homebrew prefixes,
    # each candidate probed with `--version`. The dependency ships wheels for
    # linux aarch64/x86_64/riscv64 (glibc + musl), macOS x86_64/arm64 and Windows
    # amd64/arm64, so the binary tier normally wins; on a platform without a
    # wheel uv would build ripgrep from source, which is why the Python walk
    # remains the fallback rather than an error.
    path_env_key: str
    # Empty string means ~/.sherry/runtime/ripgrep/<platform>-<arch>.
    runtime_dir: str
    version_probe_timeout_ms: int
    # SIGTERM grace, then SIGKILL grace, before the handle is abandoned. Both
    # stay well inside the tool's own scan budget so a wedged child cannot
    # outlive the call it belongs to.
    terminate_grace_seconds: float
    kill_grace_seconds: float


RIPGREP: RipgrepConfig = {
    "enabled": True,
    "path_env_key": "SHERRY_RG_PATH",
    "runtime_dir": "",
    "version_probe_timeout_ms": 5_000,
    "terminate_grace_seconds": 5.0,
    "kill_grace_seconds": 5.0,
}
