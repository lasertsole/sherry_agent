"""ast-grep structural search configuration.

The release-asset SHA-256 values below were computed on 2026-09-23 by
downloading each pinned ``0.43.0`` archive from the official GitHub release
(``https://github.com/ast-grep/ast-grep/releases/tag/0.43.0``) and hashing the
bytes with ``sha256sum``. The release publishes no checksums asset, so the
authoritative value for each platform is the digest of its official artifact.
Every entry was verified this way; there are no unverified placeholders, and the
provisioner refuses to install on any mismatch (fail-closed).
"""

from typing import TypedDict


class AstGrepConfig(TypedDict):
    """Configuration for ast-grep structural search and rewrite tools."""

    ast_grep_max_matches: int
    ast_grep_max_pattern_bytes: int
    ast_grep_timeout_ms: int
    ast_grep_max_paths: int
    ast_grep_supported_languages: list[str]
    ast_grep_strictness_default: str  # cst | smart | ast | relaxed | signature
    # ── binary provision ──
    ast_grep_pinned_version: str
    ast_grep_runtime_dir: str  # filled at runtime with ~/.sherry/runtime/ast-grep/<slug>
    ast_grep_path_env_key: str  # SHERRY_SG_PATH
    ast_grep_provision_timeout_s: float
    ast_grep_version_probe_timeout_ms: int
    ast_grep_release_assets: dict[str, dict[str, str]]  # slug → {url, sha256}


AST_GREP: AstGrepConfig = {
    "ast_grep_max_matches": 50,
    "ast_grep_max_pattern_bytes": 16384,
    "ast_grep_timeout_ms": 30000,
    "ast_grep_max_paths": 64,
    "ast_grep_supported_languages": [
        "python",
        "typescript",
        "tsx",
        "javascript",
        "rust",
        "go",
        "c",
        "cpp",
        "csharp",
        "java",
        "ruby",
        "html",
        "css",
        "json",
        "yaml",
        "bash",
        "lua",
        "swift",
        "kotlin",
        "scala",
        "php",
        "elixir",
        "haskell",
        "solidity",
    ],
    "ast_grep_strictness_default": "smart",
    # ── binary provision ──
    "ast_grep_pinned_version": "0.43.0",
    "ast_grep_runtime_dir": "",  # resolved at runtime under ~/.sherry/runtime/ast-grep
    "ast_grep_path_env_key": "SHERRY_SG_PATH",
    "ast_grep_provision_timeout_s": 60.0,
    "ast_grep_version_probe_timeout_ms": 5000,
    # GitHub release assets — 6 platforms × SHA-256 (verified against official
    # artifacts; see the module docstring for methodology).
    "ast_grep_release_assets": {
        "darwin-arm64": {
            "url": (
                "https://github.com/ast-grep/ast-grep/releases/download/0.43.0/"
                "app-aarch64-apple-darwin.zip"
            ),
            "sha256": "8c847d0a29aa4b3101b3361e0b3ee7fb53c7e497adc9ed1afc9615538cd40782",
        },
        "darwin-x64": {
            "url": (
                "https://github.com/ast-grep/ast-grep/releases/download/0.43.0/"
                "app-x86_64-apple-darwin.zip"
            ),
            "sha256": "6d703090b106747b2f56086b6ccc7e798fe78bcae70257aa20519b220153555b",
        },
        "linux-arm64": {
            "url": (
                "https://github.com/ast-grep/ast-grep/releases/download/0.43.0/"
                "app-aarch64-unknown-linux-gnu.zip"
            ),
            "sha256": "e706846148493967f3ab8011334817edd86ce5acbec10718b2a7b40799c640ff",
        },
        "linux-x64": {
            "url": (
                "https://github.com/ast-grep/ast-grep/releases/download/0.43.0/"
                "app-x86_64-unknown-linux-gnu.zip"
            ),
            "sha256": "a26253a9c821d935f7e383e40f0de7c2ca62a4121de1f73a6d81ec32eae631e0",
        },
        "win32-arm64": {
            "url": (
                "https://github.com/ast-grep/ast-grep/releases/download/0.43.0/"
                "app-aarch64-pc-windows-msvc.zip"
            ),
            "sha256": "a519fdd90324bf6858fde2d3feb2b862d67b834dc11af8f5b6c2c8143ab6a6c5",
        },
        "win32-x64": {
            "url": (
                "https://github.com/ast-grep/ast-grep/releases/download/0.43.0/"
                "app-x86_64-pc-windows-msvc.zip"
            ),
            "sha256": "a4febbc8c48671e5729d85e29e4ebe5a051b7250d19545bca18e725ccf40ef61",
        },
    },
}
