"""Interface-level diff between two revisions of one file (symbol surface).

An isolated merge's per-file CAS is revision-aware and semantically blind: a
child that renames ``foo()`` to ``bar()`` in ``utils.py`` merges with zero
conflicts, while a second child's new file still calls ``foo()`` — two clean
merges, one broken tree. This module answers the missing question for the merge
report: which functions / methods / classes disappeared, appeared, or were
renamed between the two revisions.

Pure by construction: bytes in, a delta out. No disk, no DB, no exceptions —
an oversized file or a syntax error is recorded in the delta's ``error`` field,
because the merge that called this must never fail on its report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config.features import CODE_INTEL

from .extract import extract_symbols
from .indexer import levenshtein

__all__ = [
    "InterfaceDelta",
    "RenameChange",
    "SymbolChange",
    "diff_file_interface",
    "language_for_extension",
]


@dataclass(frozen=True, slots=True)
class SymbolChange:
    """One added or removed symbol definition."""

    name: str
    kind: str  # "function" | "method" | "class"
    parent: str | None  # the enclosing class, for methods
    line: int


@dataclass(frozen=True, slots=True)
class RenameChange:
    """A removed+added pair close enough to read as a rename."""

    old_name: str
    new_name: str
    kind: str
    parent: str | None
    old_line: int
    new_line: int


@dataclass(slots=True)
class InterfaceDelta:
    """What changed between two revisions of one file's symbol surface."""

    relpath: str
    language: str
    added: list[SymbolChange] = field(default_factory=list)
    removed: list[SymbolChange] = field(default_factory=list)
    renamed: list[RenameChange] = field(default_factory=list)
    error: str | None = None

    @property
    def has_changes(self) -> bool:
        """True when the symbol surface differs (a rename counts once)."""
        return bool(self.added or self.removed or self.renamed)


def language_for_extension(suffix: str) -> str | None:
    """The tree-sitter language for a file suffix, or ``None`` (not indexed)."""
    return CODE_INTEL["code_intel_language_map"].get(suffix.lower())


def _symbols_of(data: bytes, language: str) -> tuple[list[SymbolChange], str | None]:
    """Extract one revision's symbols; ``(symbols, error)``, never raises."""
    result = extract_symbols(data, language)
    if result.error:
        return [], result.error
    changes: list[SymbolChange] = []
    for symbol in result.symbols:
        parent: str | None = None
        if symbol.parent_index is not None and 0 <= symbol.parent_index < len(result.symbols):
            parent = result.symbols[symbol.parent_index].name
        changes.append(
            SymbolChange(name=symbol.name, kind=symbol.kind, parent=parent, line=symbol.line_start)
        )
    return changes, None


def diff_file_interface(
    old_bytes: bytes | None,
    new_bytes: bytes | None,
    language: str,
    relpath: str,
    *,
    rename_threshold: int = 2,
    max_file_bytes: int = 1_000_000,
) -> InterfaceDelta | None:
    """The symbol-level delta between two revisions of one file.

    ``old_bytes=None`` reads as a creation (everything is ``added``) and
    ``new_bytes=None`` as a deletion (everything is ``removed``); both ``None``
    is nothing to compare and answers ``None``. An oversized revision or a
    syntax error lands in ``error`` instead of raising — the caller is a merge.
    """
    if old_bytes is None and new_bytes is None:
        return None

    delta = InterfaceDelta(relpath=relpath, language=language)
    for data in (old_bytes, new_bytes):
        if data is not None and len(data) > max_file_bytes:
            delta.error = f"file exceeds the interface-diff size cap ({len(data)} bytes)"
            return delta

    old_symbols: list[SymbolChange] = []
    new_symbols: list[SymbolChange] = []
    if old_bytes is not None:
        old_symbols, error = _symbols_of(old_bytes, language)
        if error:
            delta.error = error
            return delta
    if new_bytes is not None:
        new_symbols, error = _symbols_of(new_bytes, language)
        if error:
            delta.error = error
            return delta

    delta.added, delta.removed, delta.renamed = _diff_symbol_sets(
        old_symbols, new_symbols, rename_threshold=rename_threshold
    )
    return delta


def _identity(symbol: SymbolChange) -> tuple[str, str, str | None]:
    """What makes two symbols "the same symbol" across revisions."""
    return (symbol.kind, symbol.name, symbol.parent)


def _diff_symbol_sets(
    old_symbols: list[SymbolChange],
    new_symbols: list[SymbolChange],
    *,
    rename_threshold: int,
) -> tuple[list[SymbolChange], list[SymbolChange], list[RenameChange]]:
    """Exact identity diff first, then greedy rename pairing.

    Rename detection is heuristic and fail-safe: a pair is only called a rename
    when both sides sit in the same ``(kind, parent)`` group (a method never
    pairs with a free function, nor one class's method with another's) and the
    names are within *rename_threshold* edits. Everything unpaired stays in
    ``removed`` / ``added`` — the report errs towards telling the parent more.

    :returns: ``(added, removed, renamed)``.
    """
    old_by_identity = {_identity(symbol): symbol for symbol in old_symbols}
    new_by_identity = {_identity(symbol): symbol for symbol in new_symbols}

    removed = [old_by_identity[key] for key in old_by_identity.keys() - new_by_identity.keys()]
    added = [new_by_identity[key] for key in new_by_identity.keys() - old_by_identity.keys()]

    # Candidate pairs, nearest names first, so a whole family rename
    # (getX -> fetchX) still pairs one-to-one instead of crossing over.
    candidates: list[tuple[int, str, str, SymbolChange, SymbolChange]] = []
    for old_symbol in removed:
        for new_symbol in added:
            if (old_symbol.kind, old_symbol.parent) != (new_symbol.kind, new_symbol.parent):
                continue
            distance = levenshtein(old_symbol.name, new_symbol.name)
            if distance <= rename_threshold:
                candidates.append(
                    (distance, old_symbol.name, new_symbol.name, old_symbol, new_symbol)
                )
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))

    renamed: list[RenameChange] = []
    paired_old: set[tuple[str, str, str | None]] = set()
    paired_new: set[tuple[str, str, str | None]] = set()
    for _distance, _old_name, _new_name, old_symbol, new_symbol in candidates:
        old_key = _identity(old_symbol)
        new_key = _identity(new_symbol)
        if old_key in paired_old or new_key in paired_new:
            continue
        paired_old.add(old_key)
        paired_new.add(new_key)
        renamed.append(
            RenameChange(
                old_name=old_symbol.name,
                new_name=new_symbol.name,
                kind=old_symbol.kind,
                parent=old_symbol.parent,
                old_line=old_symbol.line,
                new_line=new_symbol.line,
            )
        )

    remaining_added = sorted(
        (symbol for symbol in added if _identity(symbol) not in paired_new),
        key=lambda symbol: (symbol.line, symbol.name),
    )
    remaining_removed = sorted(
        (symbol for symbol in removed if _identity(symbol) not in paired_old),
        key=lambda symbol: (symbol.line, symbol.name),
    )
    renamed.sort(key=lambda change: (change.old_line, change.old_name))
    return remaining_added, remaining_removed, renamed
