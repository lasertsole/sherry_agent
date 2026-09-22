"""Code intelligence configuration (tree-sitter symbol index + call graph)."""

from typing import TypedDict


class CodeIntelConfig(TypedDict):
    """Configuration for the subagent-only code intelligence tools.

    The index database path is intentionally left empty here and resolved at
    call time from :data:`config.path.CODE_INTEL_DIR` (mirroring the plan's
    runtime-path convention; ``config.features`` stays dependency-free).
    """

    code_intel_index_db_path: str
    code_intel_index_max_files: int
    code_intel_index_max_file_bytes: int
    code_intel_index_timeout_s: int
    code_intel_index_batch_size: int
    code_intel_explore_max_symbols: int
    code_intel_explore_max_source_chars: int
    code_intel_call_graph_max_depth: int
    code_intel_fuzzy_min_score: float
    code_intel_supported_extensions: list[str]
    code_intel_language_map: dict[str, str]
    code_intel_prune_dirs: list[str]
    code_intel_source_snippet_lines: int


CODE_INTEL: CodeIntelConfig = {
    "code_intel_index_db_path": "",  # set at runtime from config.path
    "code_intel_index_max_files": 5000,
    # Hard per-file ceiling: a larger file is skipped with a recorded warning
    # instead of being read into memory (the host has a small memory budget).
    "code_intel_index_max_file_bytes": 1_000_000,
    "code_intel_index_timeout_s": 60,
    "code_intel_index_batch_size": 100,
    "code_intel_explore_max_symbols": 10,
    "code_intel_explore_max_source_chars": 8000,
    "code_intel_call_graph_max_depth": 3,
    "code_intel_fuzzy_min_score": 0.3,
    "code_intel_supported_extensions": [
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".rs",
        ".go",
    ],
    "code_intel_language_map": {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".js": "javascript",
        ".jsx": "jsx",
        ".rs": "rust",
        ".go": "go",
    },
    "code_intel_prune_dirs": [
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        ".codeintel",
        ".codegraph",
        ".mypy_cache",
        ".ruff_cache",
    ],
    "code_intel_source_snippet_lines": 40,
}
