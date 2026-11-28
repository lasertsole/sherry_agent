"""Execution-variable table (prototype ``src/app/execution_environment.ts``).

The actual env construction lives in ``infra.environment``; this module keeps
the P2 module surface stable for adapters and documents the contract.
"""

from __future__ import annotations

from superagent.core.model import Ticket
from superagent.infra.environment import (
    CREDENTIAL_ENV,
    PREFIX,
    ExecutionEnvironment,
    execution_environment,
    worker_env,
    write_worker_environment,
)

VARIABLES = (
    "DIR",
    "NAME",
    "TICKET",
    "BLOCKED_COMMANDS",
    "GITHUB_REPOSITORY",
    "GITHUB_ACCOUNT",
    "GITHUB_COMMIT_EMAIL",
    "GITHUB_NUMBER",
    "GITHUB_PR_HEAD",
    "FEISHU_BOT",
    "FEISHU_CHAT",
)


def ticket_environment(ticket: Ticket, variables: dict[str, str] | None = None) -> dict[str, str]:
    """Return only the ``DSH_SUPERAGENT_*`` variables for a Ticket."""
    return execution_environment(ticket, variables=variables).as_dict()


__all__ = [
    "CREDENTIAL_ENV",
    "PREFIX",
    "VARIABLES",
    "ExecutionEnvironment",
    "execution_environment",
    "ticket_environment",
    "worker_env",
    "write_worker_environment",
]
