"""Evidence-ledger auto-recording configuration.

The fail-open auto-recording path is always on: terminal / python_repl runs
that look like verification commands become ledger evidence, and file edits
append a stale event for the edited path. The verification-command taxonomy
lives here (not in the tool modules) so it is data, not code.
"""

from typing import TypedDict


class EvidenceLedgerConfig(TypedDict):
    """The verification-command taxonomy for the always-on evidence recorder."""

    verify_commands: dict[str, list[str]]


EVIDENCE_LEDGER: EvidenceLedgerConfig = {
    "verify_commands": {
        "test": ["pytest", "jest", "vitest", "cargo test", "go test", "npm test"],
        "lint": ["ruff", "eslint", "flake8", "pylint", "clippy"],
        "build": ["cargo build", "npm run build", "make", "cmake"],
        "typecheck": ["basedpyright", "mypy", "tsc", "pyright"],
        "format": ["ruff format", "prettier", "black"],
    },
}
