"""LSP server configuration — binary paths, timeouts, discovery and install hints.

The four base fields (``lsp_*_server`` / timeouts / enabled languages) come from
the original Phase 2 plan; the supplement fields add binary discovery, repo-local
marker gating, and auto-install. The trailing resource fields exist because a
language server is a **heavy resident subprocess** (basedpyright/rust-analyzer are
hundreds of MB), so the manager must bound concurrency and idle lifetime.
"""

from typing import TypedDict


class LspServerSpec(TypedDict):
    """Command line, file extensions, and optional project-local install for a server.

    ``language_id`` is the protocol-level identifier sent in
    ``textDocument/didOpen.languageId`` — it is **not** always the config key
    (Bash is configured as ``bash`` but must be announced as ``shellscript``).
    """

    command: list[str]
    extensions: list[str]
    local_install: str | None
    language_id: str


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
    lsp_diagnostics_timeout_s: float
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
    "lsp_diagnostics_timeout_s": 15.0,
    # Enabled languages gate the `not_configured` state; a language listed here is
    # only usable once `resolve_lsp_server()` actually discovers its binary, so
    # this is a wish-list, not a provisioning step (nothing is installed).
    "lsp_enabled_languages": [
        "python",
        "typescript",
        "rust",
        "go",
        "cpp",
        "java",
        "ruby",
        "bash",
        "vue",
        "yaml",
    ],
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
        "ruby": {
            "markers": ["Gemfile", "Gemfile.lock", "Rakefile"],
            "bin_dirs": ["bin", "exe", "vendor/bundle/bin"],
        },
        "bash": {
            "markers": [
                "package.json",
                "bun.lock",
                "package-lock.json",
                "yarn.lock",
                "pnpm-lock.yaml",
            ],
            "bin_dirs": ["node_modules/.bin"],
        },
        "vue": {
            "markers": [
                "package.json",
                "bun.lock",
                "package-lock.json",
                "yarn.lock",
                "pnpm-lock.yaml",
            ],
            "bin_dirs": ["node_modules/.bin"],
        },
        "yaml": {
            "markers": [
                "package.json",
                "bun.lock",
                "package-lock.json",
                "yarn.lock",
                "pnpm-lock.yaml",
            ],
            "bin_dirs": ["node_modules/.bin"],
        },
    },
    # language → LSP server command + file extensions + optional local install
    "lsp_supported_servers": {
        "python": {
            "command": ["basedpyright-langserver", "--stdio"],
            "extensions": [".py", ".pyi"],
            "local_install": "uv add --dev basedpyright",
            "language_id": "python",
        },
        "typescript": {
            "command": ["typescript-language-server", "--stdio"],
            "extensions": [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"],
            "local_install": "bun add -d typescript-language-server typescript",
            "language_id": "typescript",
        },
        "rust": {
            "command": ["rust-analyzer"],
            "extensions": [".rs"],
            "local_install": None,  # toolchain-level, no project-local install
            "language_id": "rust",
        },
        "go": {
            "command": ["gopls"],
            "extensions": [".go"],
            "local_install": None,
            "language_id": "go",
        },
        "cpp": {
            "command": ["clangd"],
            "extensions": [".c", ".cpp", ".cc", ".cxx", ".h", ".hpp"],
            "local_install": None,
            "language_id": "cpp",
        },
        "java": {
            "command": ["jdtls"],
            "extensions": [".java"],
            "local_install": None,
            "language_id": "java",
        },
        "ruby": {
            "command": ["ruby-lsp"],
            "extensions": [".rb", ".rake"],
            "local_install": "bundle add ruby-lsp",
            "language_id": "ruby",
        },
        "bash": {
            "command": ["bash-language-server", "start"],
            "extensions": [".sh", ".bash", ".zsh"],
            "local_install": "npm install -D bash-language-server",
            "language_id": "shellscript",
        },
        "vue": {
            "command": ["vue-language-server", "--stdio"],
            "extensions": [".vue"],
            "local_install": "npm install -D @vue/language-server",
            "language_id": "vue",
        },
        "yaml": {
            "command": ["yaml-language-server", "--stdio"],
            "extensions": [".yaml", ".yml"],
            "local_install": "npm install -D yaml-language-server",
            "language_id": "yaml",
        },
    },
    # install hints (returned when not_installed)
    "lsp_install_hints": {
        "python": "pip install basedpyright",
        "typescript": "npm install -g typescript-language-server typescript",
        "rust": "rustup component add rust-analyzer",
        "go": "go install golang.org/x/tools/gopls@latest",
        "cpp": "See https://clangd.llvm.org/installation",
        "java": "See https://github.com/eclipse-jdtls/eclipse.jdt.ls",
        "ruby": "gem install ruby-lsp",
        "bash": "npm install -g bash-language-server",
        "vue": "npm install -g @vue/language-server",
        "yaml": "npm install -g yaml-language-server",
    },
    # auto-install commands (only executed when lsp_auto_install=True)
    "lsp_auto_install_commands": {
        "python": ["pip", "install", "basedpyright"],
        "typescript": ["npm", "install", "-g", "typescript-language-server", "typescript"],
        "go": ["go", "install", "golang.org/x/tools/gopls@latest"],
        "ruby": ["gem", "install", "ruby-lsp"],
        "bash": ["npm", "install", "-g", "bash-language-server"],
        "vue": ["npm", "install", "-g", "@vue/language-server"],
        "yaml": ["npm", "install", "-g", "yaml-language-server"],
        # rust-analyzer goes through rustup, not pip/npm; clangd/jdtls are
        # system packages — no auto-install entry for those three.
    },
    # ── resource bounds ──
    "lsp_runtime_dir": "",  # empty → ~/.sherry/runtime/lsp/<slug>
    "lsp_max_concurrent_servers": 2,
    "lsp_idle_shutdown_s": 300.0,
    "lsp_max_results": 50,
    "lsp_max_opened_files": 32,
    "lsp_max_file_bytes": 1_000_000,
}
