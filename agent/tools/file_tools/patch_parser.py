"""Parser for the Codex-compatible multi-file patch format.

The format is line-oriented and deliberately small::

    *** Begin Patch
    *** Add File: src/new.py
    +import os
    +print("hello")
    *** Update File: src/existing.py
    @@
     def example():
    -    return None
    +    return "updated"
    *** Delete File: src/old.py
    *** Update File: src/moved.py
    *** Move to: src/renamed.py
    @@
    -old
    +new
    *** End Patch

Design notes:

* **Strict headers, tolerant bodies.** A body line that is not a valid hunk
  prefix is a parse error, never silently dropped — a mangled patch must fail
  before any file is touched.
* **Paths are validated here.** Absolute paths, drive-letter paths and any
  ``..`` component are refused; a patch can only name files under the session's
  project root (the per-operation resolver still applies the external/self-path
  gates).
* **Caps.** A patch is model output; bounds on bytes, operation count, hunk
  count and added lines keep a pathological document from exhausting memory.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "MAX_PATCH_BYTES",
    "MAX_OPERATIONS",
    "MAX_HUNKS_PER_FILE",
    "MAX_ADD_LINES",
    "HunkLine",
    "Hunk",
    "PatchOperation",
    "PatchParseError",
    "parse_patch",
    "validate_patch_path",
]

MAX_PATCH_BYTES = 1_000_000
MAX_OPERATIONS = 100
MAX_HUNKS_PER_FILE = 100
MAX_ADD_LINES = 20_000

_BEGIN = "*** Begin Patch"
_END = "*** End Patch"
_ADD = "*** Add File:"
_UPDATE = "*** Update File:"
_DELETE = "*** Delete File:"
_MOVE = "*** Move to:"
_HUNK = "@@"


class PatchParseError(ValueError):
    """A syntax/path error in a patch document, with its 1-based line."""

    def __init__(self, line: int, reason: str) -> None:
        super().__init__(f"line {line}: {reason}")
        self.line = line
        self.reason = reason


@dataclass(frozen=True)
class HunkLine:
    """One hunk body line: ``kind`` is ``' '``, ``'+'`` or ``'-'``."""

    kind: str
    text: str


@dataclass(frozen=True)
class Hunk:
    """One ``@@``-delimited group; ``heading`` is the text after ``@@``."""

    heading: str
    lines: tuple[HunkLine, ...]

    @property
    def old_text(self) -> str:
        """The text this hunk expects to find (context + removed lines)."""
        return "\n".join(line.text for line in self.lines if line.kind != "+")

    @property
    def new_text(self) -> str:
        """The text this hunk produces (context + added lines)."""
        return "\n".join(line.text for line in self.lines if line.kind != "-")

    @property
    def has_change(self) -> bool:
        return any(line.kind in {"+", "-"} for line in self.lines)


@dataclass(frozen=True)
class PatchOperation:
    """One file operation parsed from the patch."""

    kind: str  # add | update | delete | move
    path: str
    header_line: int
    dest_path: str | None = None
    content_lines: tuple[str, ...] = ()
    hunks: tuple[Hunk, ...] = ()
    trailing_newline: bool = True


def validate_patch_path(raw: str, line: int) -> str:
    """Normalize and validate a patch path; raise :class:`PatchParseError`.

    Backslashes are treated as separators (a model on Windows emits them), and
    the result is a POSIX-style relative path.
    """
    path = str(raw or "").strip().replace("\\", "/")
    if not path:
        raise PatchParseError(line, "empty file path")
    if "\x00" in path:
        raise PatchParseError(line, "file path contains a NUL byte")
    if path.startswith("/") or (len(path) >= 2 and path[1] == ":"):
        raise PatchParseError(line, f"absolute paths are not allowed: {raw!r}")
    parts = [part for part in path.split("/") if part not in {"", "."}]
    if not parts:
        raise PatchParseError(line, f"invalid file path: {raw!r}")
    if ".." in parts:
        raise PatchParseError(line, f"'..' path components are not allowed: {raw!r}")
    return "/".join(parts)


def _header_path(line: str, prefix: str, number: int) -> str:
    if not line.startswith(prefix):
        raise PatchParseError(number, f"expected {prefix!r}")
    raw = line[len(prefix) :].strip()
    if not raw:
        raise PatchParseError(number, f"missing path after {prefix!r}")
    return validate_patch_path(raw, number)


def parse_patch(text: str) -> list[PatchOperation]:
    """Parse *text* into operations; raise :class:`PatchParseError` on a bad one."""
    if text is None:
        raise PatchParseError(1, "empty patch")
    if len(text.encode("utf-8", "replace")) > MAX_PATCH_BYTES:
        raise PatchParseError(1, f"patch exceeds {MAX_PATCH_BYTES} bytes")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    index = 0
    # Allow leading blank lines before the begin marker.
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines) or not lines[index].startswith(_BEGIN):
        raise PatchParseError(index + 1, f"patch must start with {_BEGIN!r}")
    index += 1

    operations: list[PatchOperation] = []
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith(_END):
            index += 1
            # Only blank lines may follow the end marker.
            for trailing in lines[index:]:
                if trailing.strip():
                    raise PatchParseError(index + 1, f"unexpected content after {_END!r}")
                index += 1
            break
        number = index + 1
        if line.startswith(_ADD):
            path = _header_path(line, _ADD, number)
            index += 1
            content: list[str] = []
            end_marker = len("\r\n")  # placeholder to keep the loop readable
            del end_marker
            while index < len(lines) and not lines[index].startswith("*** "):
                body = lines[index]
                if body.startswith("+"):
                    content.append(body[1:])
                elif body == "":
                    # Tolerant: a bare blank line is an empty content line.
                    content.append("")
                else:
                    raise PatchParseError(
                        index + 1,
                        "Add File body lines must start with '+' (blank line allowed)",
                    )
                index += 1
                if len(content) > MAX_ADD_LINES:
                    raise PatchParseError(number, f"Add File exceeds {MAX_ADD_LINES} lines")
            operations.append(
                PatchOperation(
                    kind="add",
                    path=path,
                    header_line=number,
                    content_lines=tuple(content),
                )
            )
            if len(operations) > MAX_OPERATIONS:
                raise PatchParseError(number, f"patch exceeds {MAX_OPERATIONS} operations")
            continue

        if line.startswith(_DELETE):
            path = _header_path(line, _DELETE, number)
            index += 1
            operations.append(PatchOperation(kind="delete", path=path, header_line=number))
            if len(operations) > MAX_OPERATIONS:
                raise PatchParseError(number, f"patch exceeds {MAX_OPERATIONS} operations")
            continue

        if line.startswith(_UPDATE):
            path = _header_path(line, _UPDATE, number)
            index += 1
            dest_path: str | None = None
            if index < len(lines) and lines[index].startswith(_MOVE):
                dest_path = _header_path(lines[index], _MOVE, index + 1)
                index += 1
            hunks: list[Hunk] = []
            while index < len(lines) and not lines[index].startswith("*** "):
                body = lines[index]
                if body.startswith(_HUNK):
                    heading = body[len(_HUNK) :].strip()
                    index += 1
                    hunk_lines: list[HunkLine] = []
                    while index < len(lines):
                        candidate = lines[index]
                        if candidate.startswith("*** ") or candidate.startswith(_HUNK):
                            break
                        if candidate.startswith("+"):
                            hunk_lines.append(HunkLine("+", candidate[1:]))
                        elif candidate.startswith("-"):
                            hunk_lines.append(HunkLine("-", candidate[1:]))
                        elif candidate.startswith(" "):
                            hunk_lines.append(HunkLine(" ", candidate[1:]))
                        elif candidate == "":
                            hunk_lines.append(HunkLine(" ", ""))
                        else:
                            raise PatchParseError(
                                index + 1,
                                "hunk lines must start with ' ', '+' or '-'",
                            )
                        index += 1
                    hunks.append(Hunk(heading=heading, lines=tuple(hunk_lines)))
                    if len(hunks) > MAX_HUNKS_PER_FILE:
                        raise PatchParseError(
                            number, f"{path}: more than {MAX_HUNKS_PER_FILE} hunks"
                        )
                    continue
                if body.strip() == "":
                    index += 1
                    continue
                raise PatchParseError(
                    index + 1,
                    "Update File body must be '@@' hunks prefixed with ' ', '+' or '-'",
                )
            kind = "move" if dest_path is not None else "update"
            operations.append(
                PatchOperation(
                    kind=kind,
                    path=path,
                    header_line=number,
                    dest_path=dest_path,
                    hunks=tuple(hunks),
                )
            )
            if len(operations) > MAX_OPERATIONS:
                raise PatchParseError(number, f"patch exceeds {MAX_OPERATIONS} operations")
            continue

        if line.startswith(_MOVE):
            raise PatchParseError(number, "'*** Move to:' must follow '*** Update File:'")
        raise PatchParseError(number, f"unexpected line: {line[:80]!r}")

    else:
        raise PatchParseError(len(lines), f"missing {_END!r}")

    if not operations:
        raise PatchParseError(1, "patch contains no operations")
    for operation in operations:
        if operation.kind in {"update", "move"} and not operation.hunks:
            if operation.kind == "update":
                raise PatchParseError(
                    operation.header_line,
                    f"Update File {operation.path!r} has no hunks (use Move to for a pure move)",
                )
            # A move-only Update (header + Move to) is valid: content unchanged.
        if operation.kind == "add" and operation.hunks:
            raise PatchParseError(operation.header_line, "Add File cannot carry hunks")
        if operation.kind == "delete" and operation.hunks:
            raise PatchParseError(operation.header_line, "Delete File cannot carry hunks")
    return operations
