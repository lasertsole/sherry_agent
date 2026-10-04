from __future__ import annotations

import codecs
from pathlib import Path

#: Directory names the walker refuses to descend into, at any depth. Dot-named
#: directories are skipped wholesale by :func:`should_skip_dir`, so only the
#: non-dot entries need naming here. Kept as a constant because the ripgrep
#: backend builds equivalent ``--glob`` exclusions from it — one list, two
#: engines, no drift.
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {"__pycache__", "node_modules", "venv", ".venv", ".git", ".hg", ".svn", ".idea", ".mypy_cache"}
)

#: How much of a file the binary sniff looks at. A NUL byte anywhere in the
#: sample is decisive; a UTF-8 decode failure inside it is the fallback.
_SNIFF_BYTES = 8192


def is_text_file(path: Path, sample_size: int = 4096) -> bool:
    """Check if a file appears to be a text file (no null bytes in sample)."""
    try:
        with path.open("rb") as f:
            chunk = f.read(sample_size)
        if b"\x00" in chunk:
            return False
        return True
    except (OSError, PermissionError):
        return False


def sniff_text_encoding(data: bytes) -> str | None:
    """The encoding *data* is text in, or ``None`` when it is not editable text.

    Decisive cases first: a BOM names its encoding outright (UTF-8, UTF-16
    LE/BE — UTF-16 text is full of NUL bytes, so the BOM must be checked before
    any NUL heuristic). Without a BOM, a NUL byte means binary, and anything
    that is not valid UTF-8 is binary too. ``None`` says "do not treat this as
    text", never a guess at a legacy encoding: silently rewriting a file in a
    guessed codec is worse than a refusal.

    :param data: File bytes (a sample is enough; callers read fully).
    :returns: ``"utf-8"``, ``"utf-8-sig"``, ``"utf-16"``, ``"utf-16-be"``, or
        ``None``.
    """
    if data.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if data.startswith(codecs.BOM_UTF16_BE):
        # Named separately: the ``utf-16`` codec writes a LITTLE-endian BOM, so
        # a big-endian file must round-trip through its own codec.
        return "utf-16-be"
    if data.startswith(codecs.BOM_UTF16_LE):
        return "utf-16"
    if b"\x00" in data[:_SNIFF_BYTES]:
        return None
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return "utf-8"


def decode_text(data: bytes, encoding: str | None) -> str:
    """Decode *data* with a sniffed encoding (a leading BOM is consumed)."""
    enc = encoding or "utf-8"
    if enc == "utf-16-be" and data.startswith(codecs.BOM_UTF16_BE):
        data = data[len(codecs.BOM_UTF16_BE) :]
    return data.decode(enc)


def encode_text(text: str, encoding: str | None) -> bytes:
    """Encode *text* back in the encoding it was read in (BOM included)."""
    enc = encoding or "utf-8"
    if enc == "utf-16-be":
        return codecs.BOM_UTF16_BE + text.encode("utf-16-be")
    return text.encode(enc)


def should_skip_dir(d: Path) -> bool:
    """Return True for directories that should be skipped during file walking."""
    name = d.name
    if name.startswith(".") and name not in (".", ".."):
        return True
    return name in SKIP_DIR_NAMES
