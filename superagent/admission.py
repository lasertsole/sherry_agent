"""Admission table values (prototype ``src/admission.py``, doc 07 §3)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from superagent.core.model import Strength


class CellValue(StrEnum):
    """Allowed value of one admission cell."""

    EXPLICIT = "explicit"
    INFERRED = "inferred"
    IGNORE = "ignore"


@dataclass(frozen=True)
class AdmissionCell:
    """Three-cell admission row for one receiving scope."""

    listed_unaddressed: CellValue = CellValue.INFERRED
    unlisted_addressed: CellValue = CellValue.IGNORE
    unlisted_unaddressed: CellValue = CellValue.IGNORE


DEFAULT_CELL = AdmissionCell()

DEFAULT_ADMISSION: dict[str, AdmissionCell] = {
    "github/issue": AdmissionCell(),
    "github/pr": AdmissionCell(),
    "github/comment": AdmissionCell(),
    "github/ci": AdmissionCell(
        listed_unaddressed=CellValue.IGNORE,
        unlisted_addressed=CellValue.IGNORE,
        unlisted_unaddressed=CellValue.IGNORE,
    ),
}


def _cell_value(raw: Any, field: str) -> CellValue:
    try:
        return CellValue(raw)
    except (TypeError, ValueError):
        return getattr(DEFAULT_CELL, field)


def admission_table(part: dict[str, Any]) -> dict[str, AdmissionCell]:
    """Build admission rows from one Source policy part."""
    table = dict(DEFAULT_ADMISSION)
    raw_table = part.get("admission") or {}
    if not isinstance(raw_table, dict):
        return table
    for scope, raw in raw_table.items():
        if not isinstance(raw, dict):
            continue
        table[str(scope)] = AdmissionCell(
            listed_unaddressed=_cell_value(raw.get("listed_unaddressed"), "listed_unaddressed"),
            unlisted_addressed=_cell_value(raw.get("unlisted_addressed"), "unlisted_addressed"),
            unlisted_unaddressed=_cell_value(
                raw.get("unlisted_unaddressed"), "unlisted_unaddressed"
            ),
        )
    return table


def cell_of(table: dict[str, AdmissionCell], scope: str) -> AdmissionCell:
    """Return a scope's cell, falling back to the safe default."""
    return table.get(scope, DEFAULT_CELL)


def strength_of(cell: CellValue) -> Strength | None:
    """Convert a cell value to an authorization strength."""
    if cell == CellValue.EXPLICIT:
        return Strength.EXPLICIT
    if cell == CellValue.INFERRED:
        return Strength.INFERRED
    return None
