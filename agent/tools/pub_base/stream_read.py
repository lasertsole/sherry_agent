"""Stream-based line reader for oversized files.

``read_file`` used to load the WHOLE file (``f.read()`` → decode → splitlines)
before windowing 500 lines out of it: a 200 MB log rode the Python heap in
full, and the downstream context guards only act on the finished response. This
module is the fast path's bounded alternative — it scans the file in fixed-size
chunks with ``os.read``, counts every newline for an exact ``total_lines`` and
keeps ONLY the requested window (plus, at most, one clipped line head).

Design aligned with ZCode's ``readRangeStreaming``
(``adapters/src/fs/text-range-reader.ts``): the stream is read to EOF (a
``seek``-based remaining-line count would be approximate if the file changes),
but peak memory is ``O(chunk_bytes + window + line_head)`` instead of
``O(file_size)``.

Two deliberate extensions over ZCode:

* **A per-line head cap.** ZCode accumulates ``carry`` until a newline, so a
  500 MB single-line bundle still lands on the heap. Here ``max_line_chars``
  caps the retained head: the rest of that line is counted and dropped, and the
  emitted line carries the caller's clip marker with the real omitted count.
* **CRLF normalization.** Each completed line is normalized to ``\\n``; the
  terminal line keeps no newline, matching the whole-file path's
  ``splitlines(keepends=True)`` shape.

The splitter is ``\\n``-only (ZCode does the same). ``str.splitlines`` also
splits on ``\\r`` / ``\\v`` / ``\\f``; such files would count lines differently
on the two paths, which is accepted — the streaming path is the only way to
read them at all.
"""

from __future__ import annotations

import codecs
import os

from agent.tools.pub_base.file_utils import _SNIFF_BYTES, sniff_text_encoding

__all__ = ["read_line_window"]


def _sniff_head(sample: bytes) -> str | None:
    """The encoding of a file head, tolerating a character split by the chunk edge.

    ``sniff_text_encoding`` decodes the whole sample strictly, so a chunk
    boundary inside a multi-byte UTF-8 character makes a TEXT head look invalid.
    Retry with an incremental decoder (which buffers the incomplete tail); a NUL
    byte still means binary, exactly like the whole-file sniff.
    """
    encoding = sniff_text_encoding(sample)
    if encoding is not None:
        return encoding
    if b"\x00" in sample[:_SNIFF_BYTES]:
        return None
    try:
        codecs.getincrementaldecoder("utf-8")("strict").decode(sample, final=False)
    except UnicodeDecodeError:
        return None
    return "utf-8"


def read_line_window(
    fd: int,
    offset: int,
    limit: int,
    encoding: str | None = None,
    chunk_bytes: int = 65_536,
    max_line_chars: int | None = None,
    clip_marker: str = "",
) -> tuple[list[str], int, str | None, bool, set[int]]:
    """Read lines ``[offset, offset + limit)`` without loading the file whole.

    The descriptor is read sequentially from its CURRENT position to EOF in
    ``chunk_bytes`` chunks, so callers should hand over a fresh fd. Every
    newline is counted (for an exact ``total_lines``); only the window's lines
    are retained.

    ``encoding=None`` sniffs the file from its first chunk (the same
    ``sniff_text_encoding`` rules the whole-file path uses). Chunk boundaries
    inside a multi-byte character are handled by the incremental decoder.

    ``max_line_chars`` caps ONE line's retained head (``None`` = unbounded):
    the dropped tail is counted, and the emitted line ends with
    ``clip_marker.format(omitted=<dropped chars>)``. Its line number is
    returned in ``clipped_lines`` so the caller can treat it as a clipped line
    without re-deriving the original length.

    :returns: ``(page_lines, total_lines, encoding, is_binary, clipped_lines)``
        - ``page_lines``: the requested window, ``keepends=True`` except for a
          final line that has no newline;
        - ``total_lines``: exact line count (the stream is read to EOF);
        - ``encoding``: the sniffed/used codec, or ``None`` when binary;
        - ``is_binary``: a binary / non-text head was detected;
        - ``clipped_lines``: 1-based absolute line numbers whose tail was
          dropped by ``max_line_chars`` (only those inside the window).
    """
    offset = max(1, int(offset))
    limit = max(1, int(limit))
    chunk_bytes = max(1024, int(chunk_bytes))

    first = os.read(fd, chunk_bytes)
    if encoding is None:
        encoding = _sniff_head(first)
        if encoding is None:
            return [], 0, None, True, set()
    if encoding == "utf-16-be" and first.startswith(codecs.BOM_UTF16_BE):
        first = first[len(codecs.BOM_UTF16_BE) :]

    decoder = codecs.getincrementaldecoder(encoding or "utf-8")("replace")
    page: list[str] = []
    clipped_lines: set[int] = set()
    line_no = 0
    carry = ""
    carry_clipped = False
    carry_dropped = 0

    def _emit_complete(part: str) -> None:
        """Count/collect one newline-terminated line (its '\\n' is added back)."""
        nonlocal line_no
        line_no += 1
        if not (offset <= line_no < offset + limit):
            return
        body = part.rstrip("\r")
        if carry_clipped:
            body += clip_marker.format(omitted=carry_dropped)
            clipped_lines.add(line_no)
        page.append(body + "\n")

    def _feed(text: str) -> None:
        nonlocal carry, carry_clipped, carry_dropped
        if not text:
            return
        parts = (carry + text).split("\n")
        carry = parts.pop()
        for part in parts:
            _emit_complete(part)
            carry_clipped = False
            carry_dropped = 0
        if max_line_chars is not None and len(carry) > max_line_chars:
            carry_dropped += len(carry) - max_line_chars
            carry = carry[:max_line_chars]
            carry_clipped = True

    if first:
        _feed(decoder.decode(first))
    while True:
        data = os.read(fd, chunk_bytes)
        if not data:
            break
        _feed(decoder.decode(data))
    _feed(decoder.decode(b"", final=True))

    if carry or carry_clipped:
        line_no += 1
        if offset <= line_no < offset + limit:
            body = carry.rstrip("\r")
            if carry_clipped:
                body += clip_marker.format(omitted=carry_dropped)
                clipped_lines.add(line_no)
            page.append(body)

    return page, line_no, encoding, False, clipped_lines
