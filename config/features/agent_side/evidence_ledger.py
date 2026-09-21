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
    verify_commands: dict[str, list[str]]


EVIDENCE_LEDGER: EvidenceLedgerConfig = {
    "auto_record": True,
    "auto_stale": True,
    "verify_commands": {
        "test": ["pytest", "jest", "vitest", "cargo test", "go test", "npm test"],
        "lint": ["ruff", "eslint", "flake8", "pylint", "clippy"],
        "build": ["cargo build", "npm run build", "make", "cmake"],
        "typecheck": ["basedpyright", "mypy", "tsc", "pyright"],
        "format": ["ruff format", "prettier", "black"],
    },
}
