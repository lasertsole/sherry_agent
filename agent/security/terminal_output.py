"""Strip terminal control sequences from captured output (A5).

A command's stdout is not plain text: a TUI or a progress-rendering tool emits
ANSI escapes, cursor-position reports (CPR/DSR) and other control bytes that say
nothing to a reader but cost tokens in the model's context — and the CSI/OSC
grammar is an injection surface of its own, since a sequence can carry arbitrary
parameters (and, for OSC, arbitrary text) into whatever renders it.

The stripping is deliberately conservative:

* printable text, ``\\n`` and ``\\t`` survive unchanged;
* C0 control bytes are dropped, except ``\\n``/``\\t`` (``\\r`` is dropped: a
  captured ``\\r`` came from a progress bar, and keeping it makes the model read
  the same line many times over);
* CSI (``ESC [ … final``), OSC (``ESC ] … BEL | ESC \\``) and the remaining
  escape sequences (``ESC`` + intermediates + final byte — ``ESC ( B``, ``ESC =``,
  ``ESC 7``) are removed whole, so no parameter text leaks out of a sequence.

File contents are NOT run through this: a file read that legitimately contains
an escape byte must keep it.
"""

from __future__ import annotations

import re

__all__ = ["strip_control_sequences"]

#: CSI: ``ESC [`` … parameter/intermediate bytes … one final byte (``@``–``~``).
_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
#: OSC: ``ESC ]`` … terminated by BEL or by ``ESC \``. Bounded: a broken OSC
#: without a terminator must not swallow the rest of the output.
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]{0,2048}(?:\x07|\x1b\\)")
#: Every other escape sequence: ``ESC`` + optional intermediates + one final byte.
#: The wider ``@``–``_`` class it replaced (a single final byte without intermediate
#: support) let ``ESC ( B`` and ``ESC =`` through, leaving ``(B`` in the output.
#: Runs AFTER CSI/OSC, whose introducers (``[``, ``]``) are themselves final bytes.
_ESC_SEQ_RE = re.compile(r"\x1b[\x20-\x2f]*[\x30-\x7e]")
#: A carriage return OVERWRITES its line from the start, so keep only what came
#: after the last one. Deleting the byte instead would glue a progress bar
#: together ("10%\r100%" -> "10%100%") — the concatenation a reader never saw.
#: ``\r\n`` is a line TERMINATOR, not an overwrite: without the lookahead, every
#: Windows-style line lost its text and left a bare ``\n``.
_CR_OVERWRITE_RE = re.compile(r"[^\n\r]*\r(?!\n)")
#: C0 controls that never mean anything in captured text (keep ``\n`` and ``\t``).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def strip_control_sequences(text: str) -> str:
    """Return ``text`` with terminal control sequences and control bytes removed."""
    if not text:
        return text
    if "\x1b" in text:
        text = _OSC_RE.sub("", text)
        text = _CSI_RE.sub("", text)
        text = _ESC_SEQ_RE.sub("", text)
    if "\r" in text:
        text = _CR_OVERWRITE_RE.sub("", text)
    return _CONTROL_RE.sub("", text)
