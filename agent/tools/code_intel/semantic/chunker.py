"""Symbol-level chunking for the embedding index.

Pure module: symbol rows (+ optional source lines) in, embeddable text chunks
out. It reuses the Phase 1 symbol table rather than slicing arbitrary line
windows, so each vector describes one function / method / class and carries the
metadata the tool returns (file path, line range, symbol name, kind).

The chunk text is a header line naming the symbol and its location plus the
symbol body (or the stored Phase 1 snippet when the source lines are
unavailable). Everything is bounded by ``max_chars`` so a single pathological
symbol cannot blow the memory budget.
"""

from __future__ import annotations

from dataclasses import dataclass

# Kinds the Phase 1 extractor emits that are worth embedding. A "variable" kind
# (if any future grammar adds one) is intentionally excluded — it is not a
# retrievable code unit.
_EMBEDDABLE_KINDS: frozenset[str] = frozenset({"function", "method", "class"})


@dataclass(frozen=True, slots=True)
class SymbolRecord:
    """The symbol-table columns the chunker consumes."""

    id: int
    name: str
    kind: str
    file_path: str
    line_start: int
    line_end: int
    language: str
    doc_string: str | None = None
    source_snippet: str | None = None


@dataclass(frozen=True, slots=True)
class CodeChunk:
    """One embeddable unit: a bounded header + symbol body."""

    symbol_id: int
    name: str
    kind: str
    file_path: str
    line_start: int
    line_end: int
    language: str
    text: str


def build_chunk(
    record: SymbolRecord,
    lines: list[str] | None,
    *,
    max_chars: int,
) -> CodeChunk | None:
    """Build the chunk for one symbol, or ``None`` when it is not embeddable.

    ``lines`` is the decoded source of the symbol's file (or ``None`` when it
    could not be read); the stored Phase 1 ``source_snippet`` is the fallback so
    a symbol is never dropped just because its file was unavailable.
    """
    if max_chars <= 0 or record.kind not in _EMBEDDABLE_KINDS:
        return None

    body = _body(record, lines, max_chars)
    if not body:
        return None

    header = f"{record.kind} {record.name} ({record.file_path}:{record.line_start})"
    if record.doc_string and record.doc_string.strip():
        header = f"{header}\n{record.doc_string.strip()}"
    text = f"{header}\n{body}"[:max_chars]
    return CodeChunk(
        symbol_id=record.id,
        name=record.name,
        kind=record.kind,
        file_path=record.file_path,
        line_start=record.line_start,
        line_end=record.line_end,
        language=record.language,
        text=text,
    )


def _body(record: SymbolRecord, lines: list[str] | None, max_chars: int) -> str:
    """The symbol's source span, falling back to the stored snippet."""
    if lines:
        start = max(0, record.line_start - 1)
        stop = min(len(lines), record.line_end)
        if start < stop:
            joined = "\n".join(lines[start:stop]).strip()
            if joined:
                return joined[:max_chars]
    snippet = (record.source_snippet or "").strip()
    return snippet[:max_chars]
