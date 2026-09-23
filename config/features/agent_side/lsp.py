"""LSP server configuration — binary paths, timeouts, discovery and install hints.

The four base fields (``lsp_*_server`` / timeouts / enabled languages) come from
the original Phase 2 plan; the supplement fields add binary discovery, repo-local
marker gating, and auto-install. The trailing resource fields exist because a
language server is a **heavy resident subprocess** (basedpyright/rust-analyzer are
hundreds of MB), so the manager must bound concurrency and idle lifetime.
"""

from typing import TypedDict


class LspServerSpec(TypedDict):
    """Command line, file extensions, and optional project-local install for a server."""

    command: list[str]
    extensions: list[str]
    local_install: str | None


class LspRepoLocalRule(TypedDict):
    """Marker-gated repo-local bin directory discovery rule for one language.

    A ``bin_dir`` is only trusted when at least one ``marker`` file exists in the
    same directory — prevents injecting an executable through a crafted
    ``node_modules/.bin`` in a directory that is not actually a JS project.
    """

    markers: list[str]
    bin_dirs: list[str]


class LspConfig(TypedDict):
    """Configuration for LSP tools and server provisioning."""

    # ── base fields (original Phase 2 plan) ──
    lsp_python_server: str
    lsp_typescript_server: str
    lsp_rust_server: str
    lsp_go_server: str
    lsp_request_timeout_s: float
    lsp_server_start_timeout_s: float
    lsp_enabled_languages: list[str]
    # ── supplement fields (Phase 2S) ──
    lsp_install_timeout_s: float
    lsp_auto_install: bool
    lsp_resolve_cache_enabled: bool
    lsp_repo_local_bin_rules: dict[str, LspRepoLocalRule]
    lsp_supported_servers: dict[str, LspServerSpec]
    lsp_install_hints: dict[str, str]
    lsp_auto_install_commands: dict[str, list[str]]
    # ── resource bounds (heavy resident subprocess) ──
    lsp_runtime_dir: str
    lsp_max_concurrent_servers: int
    lsp_idle_shutdown_s: float
    lsp_max_results: int
    lsp_max_opened_files: int
    lsp_max_file_bytes: int


LSP: LspConfig = {
    # ── base fields (original Phase 2 plan) ──
    "lsp_python_server": "basedpyright-langserver",
    "lsp_typescript_server": "typescript-language-server",
    "lsp_rust_server": "rust-analyzer",
    "lsp_go_server": "gopls",
    "lsp_request_timeout_s": 10.0,
    "lsp_server_start_timeout_s": 15.0,
    "lsp_enabled_languages": ["python", "typescript", "rust", "go"],
    # ── supplement fields (Phase 2S) ──
    "lsp_install_timeout_s": 60.0,
    "lsp_auto_install": False,
    "lsp_resolve_cache_enabled": True,
    # repo-local bin discovery (marker-gated). A bin dir is trusted only when a
    # marker file is present at the same directory level.
    "lsp_repo_local_bin_rules": {
        "python": {
            "markers": [
                "pyproject.toml",
                "requirements.txt",
                "setup.py",
                "setup.cfg",
                "pyrightconfig.json",
            ],
            "bin_dirs": [".venv/bin", ".venv/Scripts", "venv/bin", "venv/Scripts"],
        },
        "typescript": {
            "markers": [
                "package.json",
                "bun.lock",
                "package-lock.json",
                "yarn.lock",
                "pnpm-lock.yaml",
            ],
            "bin_dirs": ["node_modules/.bin"],
        },
        "rust": {
            "markers": ["Cargo.toml"],
            "bin_dirs": ["target/debug", "target/release"],
        },
        "go": {
            "markers": ["go.mod", "go.sum", "go.work"],
            "bin_dirs": ["bin"],
        },
    },
    # language → LSP server command + file extensions + optional local install
    "lsp_supported_servers": {
        "python": {
            "command": ["basedpyright-langserver", "--stdio"],
            "extensions": [".py", ".pyi"],
            "local_install": "uv add --dev basedpyright",
        },
        "typescript": {
            "command": ["typescript-language-server", "--stdio"],
            "extensions": [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"],
            "local_install": "bun add -d typescript-language-server typescript",
        },
        "rust": {
            "command": ["rust-analyzer"],
            "extensions": [".rs"],
            "local_install": None,  # toolchain-level, no project-local install
        },
        "go": {
            "command": ["gopls"],
            "extensions": [".go"],
            "local_install": None,
        },
    },
    # install hints (returned when not_installed)
    "lsp_install_hints": {
        "python": "pip install basedpyright",
        "typescript": "npm install -g typescript-language-server typescript",
        "rust": "rustup component add rust-analyzer",
        "go": "go install golang.org/x/tools/gopls@latest",
    },
    # auto-install commands (only executed when lsp_auto_install=True)
    "lsp_auto_install_commands": {
        "python": ["pip", "install", "basedpyright"],
        "typescript": ["npm", "install", "-g", "typescript-language-server", "typescript"],
        "go": ["go", "install", "golang.org/x/tools/gopls@latest"],
        # rust-analyzer goes through rustup, not pip/npm: no auto-install entry
    },
    # ── resource bounds ──
    "lsp_runtime_dir": "",  # empty → ~/.sherry/runtime/lsp/<slug>
    "lsp_max_concurrent_servers": 2,
    "lsp_idle_shutdown_s": 300.0,
    "lsp_max_results": 50,
    "lsp_max_opened_files": 32,
    "lsp_max_file_bytes": 1_000_000,
}
