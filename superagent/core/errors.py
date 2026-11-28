"""Fixed user-facing errors (prototype ``src/core/errors.py``)."""

from __future__ import annotations

from typing import Any


class UserError(Exception):
    """An error whose code and values are safe to show to a person."""

    def __init__(self, code: str, values: dict[str, Any] | None = None) -> None:
        self.code = code
        self.values: dict[str, Any] = values or {}
        super().__init__(code)


USER_ERRORS: dict[str, str] = {
    "not_found": "The requested item was not found.",
    "not_latest": "The item is no longer the latest one.",
    "followup_owned": "A Ticket still owns this follow-up; only stop is allowed.",
    "already_ended": "The Ticket has already ended.",
    "cannot_retry": "This Ticket cannot be retried in its current stage.",
    "stale_revision": "The settings or prompt revision changed; reload and retry.",
    "unavailable": "The evaluation could not finish.",
    "invalid_value": "A supplied value is invalid.",
    "invalid_path": "A supplied path is invalid.",
    "bad_request": "The request body is malformed.",
    "unauthorized": "The request is not authorized.",
    "forbidden": "The request is not allowed from this origin.",
    "channel_gone": "The conversation no longer accepts messages.",
    "channel_ignored": "The Source does not listen on this channel.",
    "setup_required": "Setup has not completed.",
}


def error_message(code: str, values: dict[str, Any] | None = None) -> str:
    """Render the canonical message, tolerating unknown codes."""
    template = USER_ERRORS.get(code, code)
    try:
        return template.format(**(values or {}))
    except (KeyError, IndexError):
        return template


def http_status(code: str) -> int:
    """Map an error code to the HTTP status used by the management API."""
    if code == "not_found":
        return 404
    if code == "stale_revision":
        return 409
    if code in {"unauthorized", "forbidden"}:
        return 403
    if code == "setup_required":
        return 503
    return 400
