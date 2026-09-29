"""Loguru patcher that redacts secrets before any sink sees them.

Why a patcher rather than the sink wrapper the plan sketched: rewriting
``handler._sink`` reaches into loguru's private handler objects, has to be
re-run whenever a sink is added later, and silently stops working if loguru
renames the attribute. ``logger.configure(patcher=…)`` is the supported hook —
it runs for every record and every sink (console, the three rotating files, any
sink added afterwards), so redaction cannot be bypassed by adding a sink.

Scope and limits, stated plainly:

* the **message** and every **extra** value are redacted;
* an exception's traceback is rendered by loguru from the live frames, so a
  secret that only ever existed as a local variable inside the failing frame can
  still appear in the ``error`` sink's ``diagnose`` dump. Covering that would
  mean re-implementing loguru's traceback rendering; instead, log a redacted
  value (``logger.error("key {}", redact_sensitive_text(key))`` is redundant
  now — plain ``logger.error("key {}", key)`` is already redacted) and treat the
  error log as a local, access-controlled artifact.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from agent.security.redact import redact_sensitive_text

__all__ = ["install_log_redaction", "redact_record"]


def redact_record(record: dict[str, Any]) -> dict[str, Any]:
    """loguru patcher: scrub the message and the structured extras in place.

    ``force=True``: a log file outlives the session and is read by people who
    never saw the secret, so the ``SHERRY_REDACT`` switch does not apply here.
    """
    message = record.get("message")
    if message:
        record["message"] = redact_sensitive_text(str(message), force=True)

    extra = record.get("extra")
    if isinstance(extra, dict):
        for key, value in extra.items():
            if isinstance(value, str):
                extra[key] = redact_sensitive_text(value, force=True)

    return record


def install_log_redaction() -> None:
    """Install the redacting patcher on the global logger (idempotent)."""
    logger.configure(patcher=redact_record)
