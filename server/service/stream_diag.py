"""Stream-diagnostic collector — per-turn stream metadata + exception-chain flatten.

Attached to every :class:`~server.service.stream_dispatch.StreamTurn` run: it
counts streamed model chunks / text bytes and the time to the first chunk, so
a mid-stream death can be located ("died after N chunks, first chunk after
Xs") even when LangChain abstracts the underlying HTTP response away. On
failure the summary is appended to the re-raised exception message.
"""

import time
from typing import Any

from config.features import WS_STREAM

STREAM_DIAG_HEADERS = WS_STREAM["stream_diag_headers"]

_MAX_FLATTEN_DEPTH = WS_STREAM["max_flatten_depth"]


def stream_diag_init() -> dict[str, Any]:
    return {
        "started_at": time.time(),
        "first_chunk_at": None,
        "chunks": 0,
        "bytes": 0,
        "headers": {},
        "http_status": None,
    }


def flatten_exception_chain(error: BaseException) -> str:
    """Render ``error`` plus its ``__cause__``/``__context__`` chain on one line."""
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    depth = 0
    while current is not None and depth < _MAX_FLATTEN_DEPTH:
        if id(current) in seen:
            parts.append("<cycle>")
            break
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
        if current is not None:
            parts.append("<-")
        depth += 1
    return " ".join(parts)


def stream_diag_summary(diag: dict[str, Any], error: BaseException | None = None) -> str:
    """One-line diagnostic summary for logs and exception messages."""
    first_chunk = diag.get("first_chunk_at")
    ttfb = f"{first_chunk - diag['started_at']:.2f}s" if first_chunk is not None else "never"
    parts = [
        f"chunks={diag.get('chunks', 0)}",
        f"bytes={diag.get('bytes', 0)}",
        f"first_chunk_after={ttfb}",
        f"http_status={diag.get('http_status')}",
    ]
    headers = diag.get("headers") or {}
    if headers:
        parts.append(f"headers={headers}")
    if error is not None:
        parts.append(f"error={flatten_exception_chain(error)}")
    return " ".join(parts)


#: Separates the user-facing message from the triage summary in an exception.
DIAG_MARKER = " | diag: "


def reraise_with_diag(exc: Exception, summary: str) -> None:
    """Re-raise ``exc`` with ``summary`` appended; keeps the original type.

    The summary is for LOGS and triage: ``public_error_text`` strips it back off
    before anything reaches a user (the chat's failure bubble showed the raw
    ``chunks=… http_status=…`` line). Exceptions whose constructor rejects a
    single string argument are re-raised untouched so the diagnostic never masks
    the real error.
    """
    try:
        augmented = type(exc)(f"{exc}{DIAG_MARKER}{summary}")
    except TypeError:
        raise exc from None
    raise augmented from exc


def public_error_text(error: BaseException | str) -> str:
    """The client-facing part of an error message — the diag stays in the logs.

    :param error: The exception (or an already-rendered message).
    :returns: The message up to :data:`DIAG_MARKER`, or the whole text when no
        summary was appended.
    """
    text = str(error)
    head, marker, _tail = text.partition(DIAG_MARKER)
    return head if marker else text
