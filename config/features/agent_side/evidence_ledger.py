"""Evidence-ledger auto-recording configuration.

Toggles for the fail-open auto-recording path: terminal / python_repl runs
that look like verification commands become ledger evidence, and file edits
append a stale event for the edited path. The verification-command taxonomy
lives here (not in the tool modules) so it is data, not code.
"""

from typing import TypedDict


class EvidenceLedgerConfig(TypedDict):
    """Auto-record/auto-stale toggles plus the verification-command taxonomy."""

    auto_record: bool
    auto_stale: bool
    enforce_on_complete: bool
    """Whether the completion-drain middleware turns the Sisyphus reminder into
    a programmatic gate. Default False keeps the existing text-reminder path."""
    verify_commands: dict[str, list[str]]


EVIDENCE_LEDGER: EvidenceLedgerConfig = {
    "auto_record": True,
    "auto_stale": True,
    "enforce_on_complete": False,
    "verify_commands": {
        "test": ["pytest", "jest", "vitest", "cargo test", "go test", "npm test"],
        "lint": ["ruff", "eslint", "flake8", "pylint", "clippy"],
        "build": ["cargo build", "npm run build", "make", "cmake"],
        "typecheck": ["basedpyright", "mypy", "tsc", "pyright"],
        "format": ["ruff format", "prettier", "black"],
    },
}
