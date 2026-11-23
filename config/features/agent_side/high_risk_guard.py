"""High-risk operation guard configuration.

Two independent layers share this registry:

* the **operation ledger** only records tools that are both ``high_risk`` and
  non-idempotent (PTC ``execute_code`` and every MCP tool by default), so a
  verification re-run cannot execute the same irreversible call twice;
* the **keyword scan** applies to tools whose arguments can carry a dangerous
  operation (``terminal`` / ``python_repl`` / PTC / MCP). A hit asks a human
  (main agent) or bubbles ``[NEEDS_APPROVAL]`` out of a child.

The built-in keyword list is intentionally small and only covers operations the
project is willing to call dangerous out of the box; an operator/agent extends
it per session through the ``high_risk_guard`` tool or the panel. Keywords are
substring-matched case-insensitively, so every entry must be at least three
characters (``min_keyword_chars``) — a two-character entry would match nearly
every call.
"""

from typing import TypedDict


class HighRiskGuardConfig(TypedDict):
    """Keyword/ledger guard: built-in keywords, retention and length bounds."""

    enabled: bool
    #: Gates the REPLAY paths that used to run outside the middleware chain
    #: (PTC script-inner calls, LDW leaf tool calls, the PTC compile check).
    #: ``False`` restores the pre-replay-gate behaviour without a code change;
    #: ``enabled=False`` still turns the whole guard off.
    replay_path_gate_enabled: bool
    #: Hours an executed keyword/op row stays before the lazy sweep removes it.
    retention_hours: int
    min_keyword_chars: int
    max_keyword_chars: int
    #: Keywords every session starts with (policy=block). All entries are >= 3
    #: characters — see the module docstring.
    builtin_keywords: tuple[str, ...]


HIGH_RISK_GUARD: HighRiskGuardConfig = {
    "enabled": True,
    "replay_path_gate_enabled": True,
    "retention_hours": 24,
    "min_keyword_chars": 3,
    "max_keyword_chars": 100,
    "builtin_keywords": (
        # Financial / credential identifiers.
        "card_number",
        "credit_card",
        "银行卡",
        "信用卡",
        "cvv",
        "expiry_date",
        "account_number",
        "routing_number",
        "iban",
        "swift_code",
        # Authentication secrets.
        "password",
        "passwd",
        "api_key",
        "secret_key",
        "access_token",
        "private_key",
        "-----BEGIN RSA PRIVATE KEY-----",
        # Destructive system operations.
        "rm -rf",
        "mkfs",
        "dd if=",
        "shutdown",
        "reboot",
        "git push --force",
        "git push -f",
        "DROP TABLE",
        "DELETE FROM",
        "TRUNCATE",
    ),
}
