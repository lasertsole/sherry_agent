"""Loguru patcher that redacts secrets before any sink sees them.

Why a patcher rather than the sink wrapper the plan sketched: rewriting
``handler._sink`` reaches into loguru's private handler objects, has to be
re-run whenever a sink is added later, and silently stops working if loguru
renames the attribute. ``logger.configure(patcher=…)`` is the supported hook —
it runs for every record and every sink (console, the three rotating files, any
sink added afterwards), so redaction cannot be bypassed by adding a sink.

Scope:

* the **message** and every **extra** value are redacted;
* the **exception** is redacted too. loguru renders a traceback from the live
  frames at emit time, so a patcher never sees that text — which left two ways
  for a secret to reach a log file: the exception's own message
  (``RuntimeError: bad key sk-…``) and, on the error sink, the ``diagnose``
  annotations that print the values of the names on each shown source line.
  The patch therefore renders the exception here, through loguru's own
  ``ExceptionFormatter`` when it is importable — so the ``> File …`` backtrace
  layout survives — redacts that text, appends it to the message, and clears
  ``record["exception"]`` so no sink renders it a second time.

The render carries source lines but **not** variable values, i.e. no
``diagnose`` annotations, and that is deliberate rather than an oversight: the
annotations print frame locals verbatim, which is where account identifiers live
and where a value no redaction family recognises (a chat id, a name, a quoted
message) would be written to disk. ``backtrace`` stays on, so the failing line
in each frame is still there. The visible consequence: a sink whose format has no
``{exception}`` field (the console, deliberately quiet until now) prints the
traceback as part of the message — rendering it ourselves is the only way to
redact it, and a traceback on stderr is loguru's own default behaviour.
"""

from __future__ import annotations

import traceback
from typing import Any

from loguru import logger

from agent.security.redact import redact_sensitive_text

__all__ = ["install_log_redaction", "redact_record"]


def redact_record(record: dict[str, Any]) -> dict[str, Any]:
    """loguru patcher: scrub the message, the extras and the exception in place.

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

    exception = record.get("exception")
    if exception is not None and getattr(exception, "type", None) is not None:
        rendered = _render_exception(exception)
        record["message"] = (
            f"{record.get('message', '')}\n{redact_sensitive_text(rendered, force=True)}"
        )
        record["exception"] = None

    return record


def _render_exception(exception: Any) -> str:
    """Render an exception record as the sinks would, for redaction.

    Prefers loguru's own formatter (annotated ``diagnose`` output included, which
    is what the error sink is configured for); falls back to the standard library
    if loguru's internals move. The fallback loses the annotations — a cosmetic
    loss, and redaction still holds.
    """
    type_, value, tb = exception
    formatter = _loguru_formatter()
    if formatter is not None:
        return "".join(formatter.format_exception(type_, value, tb, from_decorator=False))
    return "".join(traceback.format_exception(type_, value, tb))


def _loguru_formatter() -> Any:
    """Build loguru's ``ExceptionFormatter``: source lines in, values out."""
    try:
        from loguru._better_exceptions import ExceptionFormatter
    except Exception:  # pragma: no cover - loguru internals moved
        return None
    try:
        return ExceptionFormatter(
            colorize=False,
            backtrace=True,
            diagnose=False,
            hidden_frames_filename=logger.catch.__code__.co_filename,
        )
    except Exception:  # pragma: no cover - constructor signature moved
        return None


def install_log_redaction() -> None:
    """Install the redacting patcher on the global logger (idempotent)."""
    logger.configure(patcher=redact_record)
