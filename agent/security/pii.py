"""Stable pseudonyms for channel user/chat identifiers in logs.

A chat platform hands us identifiers for the person and the chat (QQ openids,
Telegram ids, Discord snowflakes). Nothing in the agent needs them: an inbound
message is routed to a session derived from the CHANNEL name, and the reply
target travels through ``relation_register``/``reply_target``, not through a
prompt. The one place they do surface is the log — where an operator debugging a
delivery failure reads them, and where they outlive the conversation as a
record of who talked to the bot.

So the log boundary gets a pseudonym instead of the raw value:

* **stable** — the same identifier hashes to the same string in every process
  and every run, so ``chat_id=«pii:…»`` still correlates the "received" line
  with the "sent successfully" line and with a later log file;
* **truncated** — 12 hex chars of SHA-256, which is plenty to keep the
  namespace collision-free at log scale and keeps lines readable;
* **one-way** — the raw value is not recoverable, so a leaked log no longer
  discloses an account identifier.

The raw values stay untouched everywhere they function: routing tables, reply
targets, the cron sender comparison and the platform SDK calls. This is the same
split the secret redaction uses — originals first, pseudonyms only at the output
boundary.
"""

from __future__ import annotations

import hashlib

__all__ = ["PSEUDONYM_PREFIX", "hash_identifier", "pseudonym"]

#: Marker around the pseudonym, matching the secret engine's ``«redacted»``
#: sentinel: greppable, and obvious to a reader who meets it in a log.
PSEUDONYM_PREFIX = "«pii:"

_HASH_CHARS = 12


def hash_identifier(value: str) -> str:
    """Return the 12-hex-char SHA-256 digest of ``value``.

    Stable across processes and runs — that stability is the point: it is what
    lets an operator correlate two log lines without storing the identifier.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_HASH_CHARS]


def pseudonym(value: object) -> str:
    """Return the loggable pseudonym for an identifier.

    ``None``/empty pass through as ``""`` so an absent id stays visible as
    absent rather than becoming a hash of nothing.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if not text:
        return ""
    return f"{PSEUDONYM_PREFIX}{hash_identifier(text)}»"
