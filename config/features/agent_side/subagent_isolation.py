"""Worktree-isolated subagent workspaces (the only isolation mode)."""

from typing import TypedDict


class SubagentIsolationConfig(TypedDict):
    """Worktree isolation: what each isolated run materializes, and what a
    self-initialized baseline repository must never commit.

    An isolated run gets its own `git worktree` of the parent project (a dirty
    baseline built from ``git stash create``, so the child starts from exactly
    what the user's working tree holds). A worktree checkout only carries
    committed content, so the paths below — ignored by definition — are
    materialized into it afterwards: symlinked when they are shared machine
    state (a whole directory link keeps SQLite's ``-wal``/``-shm`` consistent),
    copied when two concurrent trees must not share one mutable file.

    The deny list guards ``git init`` for NON-repository projects: the patterns
    land in the fresh repository's ``.git/info/exclude`` BEFORE ``git add -A``,
    so secrets and bulk never enter the baseline commit's history (removing them
    afterwards would not help — history keeps what was committed once).
    """

    #: Paths (relative to the project root) materialized as SYMLINKS: shared
    #: state, huge, or both — one inode, so locks and WAL stay coherent.
    include_symlink: list[str]
    #: Paths materialized as COPIES: live mutable state two trees must not share.
    include_copy: list[str]
    #: Marker file name written into an auto-initialized project (documents how
    #: to undo the initialization; never committed).
    repo_marker_name: str
    #: `.git/info/exclude` patterns for the auto-init baseline commit.
    deny_patterns: list[str]
    #: Message of the auto-init baseline commit.
    baseline_commit_message: str
    #: Branch prefix of the per-run worktree branch (``sherry/<run8>``).
    branch_prefix: str
    #: Optional declaration file read from the project root; its entries extend
    #: (and may override the mode of) the tables above.
    include_file_name: str
    #: How long a `git` subprocess may take before the isolation is refused.
    git_timeout_s: float
    #: Report renamed/added/removed symbols in the merge announcement (the
    #: parent's other subagents may still call the old names).
    interface_diff_enabled: bool
    #: Per-file size cap of the interface diff; a larger revision is skipped
    #: with a recorded note instead of being read twice into memory.
    interface_diff_max_file_bytes: int
    #: Name-edit distance at which a removed+added pair reads as a rename.
    interface_diff_rename_levenshtein: int


SUBAGENT_ISOLATION: SubagentIsolationConfig = {
    # Shared machine state and generated artifacts: one link serves every tree,
    # and the merge never writes through a link it did not create.
    "include_symlink": [
        "src",
        "workspace/memory",
        "workspace/sessions",
        "cron_jobs.json",
        "skills/auto",
        "client/node_modules",
    ],
    # Live mutable task state: a boulder/progress file shared by two concurrent
    # worktrees would corrupt, so it is copied and merged back.
    "include_copy": [".omo"],
    "repo_marker_name": ".sherry-isolation-repo",
    # Secrets first (history keeps what was ever committed), then bulk, then the
    # repository's own state. `.env.example` stays as the one intentional sample.
    "deny_patterns": [
        ".env",
        ".env.*",
        "!.env.example",
        "*.pem",
        "*.key",
        "id_rsa*",
        "*_rsa",
        "*_ed25519",
        ".npmrc",
        ".pypirc",
        ".aws/",
        ".ssh/",
        "node_modules/",
        ".venv/",
        "__pycache__/",
        "*.pyc",
        "target/",
        "dist/",
        "build/",
        ".next/",
        ".cache/",
        ".sherry-isolation-repo",
    ],
    "baseline_commit_message": "sherry: isolation baseline",
    "branch_prefix": "sherry/",
    "include_file_name": ".worktreeinclude",
    "git_timeout_s": 120.0,
    "interface_diff_enabled": True,
    "interface_diff_max_file_bytes": 1_000_000,
    "interface_diff_rename_levenshtein": 2,
}
