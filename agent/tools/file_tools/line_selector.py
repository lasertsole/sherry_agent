"""GJC-style line selector parsed out of a ``read_file`` path (``path:50-200``).

The syntax comes from Claude Code: a colon suffix on the file path selects what
to read instead of paging from line 1 — a range (``:50-200``), several ranges
(``:50-200,960-973``), a single line (``:42``), git conflict hunks
(``:conflicts``), or the file without line numbers (``:raw``).

The colon is parsed from the RIGHT and only accepted when the suffix matches
the selector grammar, which is what keeps Windows drive letters working:
``C:\\file.py:50-200`` splits at the selector colon (the drive colon is not the
last one), while ``C:\\file.py`` finds the drive colon, fails the grammar
(``\\file.py`` is not a range list) and stays a path. A file whose name itself
ends in ``:conflicts`` is theoretical (``:`` is not a Windows path character and
exotic on POSIX) — the selector wins there, matching Claude Code.
"""

from __future__ import annotations

import re

__all__ = ["LineSelector", "conflict_blocks", "parse_line_selector"]

#: ``50`` / ``50-200`` / ``50-200,960-973`` — 1-based, inclusive, ascending.
_RANGE_LIST_RE = re.compile(r"^\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$")

_SELECTOR_KINDS = ("conflicts", "raw")


class LineSelector:
    """One parsed selector: what to read, independent of how it is paged."""

    __slots__ = ("ranges", "kind")

    def __init__(
        self,
        ranges: tuple[tuple[int, int], ...] = (),
        kind: str | None = None,
    ) -> None:
        self.ranges = ranges
        self.kind = kind  # None = ranges | "conflicts" | "raw"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"LineSelector(ranges={self.ranges!r}, kind={self.kind!r})"

    @property
    def wants_line_numbers(self) -> bool:
        """``:raw`` suppresses the ``LINE_NUM|`` prefix; everything else keeps it."""
        return self.kind != "raw"

    @property
    def is_selective(self) -> bool:
        """True for multi-range/conflict views: partial by construction, so they
        never license a write and are never stubbed as "unchanged"."""
        return self.kind == "conflicts" or len(self.ranges) > 1


def _parse_ranges(spec: str) -> tuple[tuple[int, int], ...] | None:
    """``"50-200,960"`` → ``((50, 200), (960, 960))``, or ``None`` if malformed."""
    ranges: list[tuple[int, int]] = []
    for part in spec.split(","):
        start_text, _, end_text = part.partition("-")
        start = int(start_text)
        end = int(end_text) if end_text else start
        if start < 1 or end < start:
            return None
        ranges.append((start, end))
    return tuple(ranges) if ranges else None


def parse_line_selector(file_path: str) -> tuple[str, LineSelector | None, str | None]:
    """Split a trailing selector off *file_path*.

    :returns: ``(path_part, selector, error)`` — the path to resolve, the
        parsed selector (``None`` when the path carries none), and an error
        message for a malformed selector (``None`` otherwise).
    """
    index = file_path.rfind(":")
    if index == -1:
        return file_path, None, None
    spec = file_path[index + 1 :]
    path_part = file_path[:index]

    if spec in _SELECTOR_KINDS:
        return path_part, LineSelector(kind=spec), None

    if not _RANGE_LIST_RE.match(spec):
        # Not a selector (a Windows drive colon, a colon inside a name): the
        # whole string stays a path and the ordinary offset/limit path runs.
        return file_path, None, None
    ranges = _parse_ranges(spec)
    if ranges is None:
        return (
            None,
            None,
            (
                f"Invalid line selector ':{spec}': ranges must be ascending and 1-based "
                "(e.g. ':50-200' or ':50-200,960-973')."
            ),
        )
    return path_part, LineSelector(ranges=ranges), None


_CONFLICT_START = "<<<<<<<"
_CONFLICT_END = ">>>>>>>"


def conflict_blocks(all_lines: list[str]) -> tuple[tuple[int, int], ...]:
    """The 1-based inclusive spans of every git conflict hunk in *all_lines*.

    A hunk runs from its ``<<<<<<<`` line to the next ``>>>>>>>`` line (an
    unterminated hunk runs to EOF — a half-resolved file is exactly when this
    selector gets used, so the partial hunk is still shown).
    """
    blocks: list[tuple[int, int]] = []
    start: int | None = None
    for index, line in enumerate(all_lines):
        stripped = line.lstrip()
        if start is None and stripped.startswith(_CONFLICT_START):
            start = index + 1
        elif start is not None and stripped.startswith(_CONFLICT_END):
            blocks.append((start, index + 1))
            start = None
    if start is not None:
        blocks.append((start, len(all_lines)))
    return tuple(blocks)
