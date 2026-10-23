"""Evidence-ledger configuration: recorder taxonomy + SQLite retention policy.

The fail-open auto-recording path is always on: terminal / python_repl runs
that look like verification commands become ledger evidence, and file edits
append a stale event for the edited path. The verification-command taxonomy
lives here (not in the tool modules) so it is data, not code. The retention
keys drive the lazy three-layer pruning on append and the explicit
session-delete / boot-sweep purges.
"""

from typing import TypedDict


class EvidenceLedgerConfig(TypedDict):
    """The recorder taxonomy plus the SQLite retention/purge policy."""

    verify_commands: dict[str, list[str]]
    # Lazy pruning on append (hermes-style three layers).
    pruning_enabled: bool
    max_age_days: int
    max_events_per_session: int
    max_total_events: int
    # Explicit deletion hooks.
    purge_on_session_delete: bool
    purge_on_boot_sweep: bool


EVIDENCE_LEDGER: EvidenceLedgerConfig = {
    "verify_commands": {
        "test": ["pytest", "jest", "vitest", "cargo test", "go test", "npm test"],
        "lint": ["ruff", "eslint", "flake8", "pylint", "clippy"],
        "build": ["cargo build", "npm run build", "make", "cmake"],
        "typecheck": ["basedpyright", "mypy", "tsc", "pyright"],
        "format": ["ruff format", "prettier", "black"],
    },
    "pruning_enabled": True,
    "max_age_days": 30,
    "max_events_per_session": 100,
    "max_total_events": 10_000,
    "purge_on_session_delete": True,
    "purge_on_boot_sweep": True,
}
