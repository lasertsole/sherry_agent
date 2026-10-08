"""Bounded scan primitives shared by both ``search_files`` modes.

Content search and file-name search walk the same tree with the same
hazards: a huge root, a pseudo-filesystem mount, or a pattern with too many
hits. :class:`ScanState` carries the per-call bounds read from
``TOOLS_TIMEOUTS`` (time budget, match cap, prune list) and renders the
result envelope with explicit cut markers; :func:`bounded_walk` applies
directory pruning and the time budget to ``os.walk``. A stopped scan is
always observable — it never looks like a complete one.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from config.features import TOOLS_TIMEOUTS

from agent.tools.pub_base import should_skip_dir

type _ScanStopReason = Literal["time_budget", "max_matches"]

_SCAN_STOP_TIME: _ScanStopReason = "time_budget"
_SCAN_STOP_MATCHES: _ScanStopReason = "max_matches"


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """One search request; mode-specific fields default to their 'off' values."""

    pattern: str
    root: Path
    file_glob: str | None = None
    context: int = 0


@dataclass(slots=True)
class ScanState:
    """Per-call scan bounds and bookkeeping for one bounded walk."""

    offset: int
    limit: int
    deadline: float
    max_matches: int
    prune_dirs: frozenset[str]
    pruned_dir_count: int = 0
    page_truncated: bool = False
    stop_reason: _ScanStopReason | None = None

    @classmethod
    def start(cls, offset: int, limit: int) -> ScanState:
        """Build a state whose bounds are read from config at call time."""
        return cls(
            offset=offset,
            limit=limit,
            deadline=time.monotonic() + TOOLS_TIMEOUTS["file_tools_search_time_budget_s"],
            max_matches=TOOLS_TIMEOUTS["file_tools_search_max_matches"],
            prune_dirs=frozenset(TOOLS_TIMEOUTS["file_tools_search_prune_dirs"]),
        )

    @property
    def stopped(self) -> bool:
        """True once the page is full or a scan bound (time / match cap) was hit."""
        return self.stop_reason is not None or self.page_truncated

    def expired(self) -> bool:
        """True when the scan time budget has been exhausted."""
        return time.monotonic() > self.deadline

    def note_match(self, found: int) -> None:
        """Record the bound state after one more match was collected."""
        if found >= self.max_matches:
            self.stop_reason = _SCAN_STOP_MATCHES
        elif found >= self.offset + self.limit + 1:
            self.page_truncated = True

    def note_budget(self) -> None:
        """Mark the scan as cut short by the time budget."""
        self.stop_reason = _SCAN_STOP_TIME

    def prune(self, dirnames: list[str], parent: Path) -> None:
        """Drop skip-listed and pseudo-filesystem dirs from the walk, in place."""
        kept: list[str] = []
        for name in dirnames:
            if should_skip_dir(parent / name):
                continue
            if name in self.prune_dirs:
                self.pruned_dir_count += 1
                continue
            kept.append(name)
        dirnames[:] = kept

    def finish[T](self, page_key: str, found: list[T]) -> dict:
        """Render the result envelope, keeping every cut visible to the model."""
        result: dict = {
            page_key: found[self.offset : self.offset + self.limit],
            "total_count": len(found),
        }
        if self.pruned_dir_count:
            result["pruned_dir_count"] = self.pruned_dir_count
        if self.page_truncated or len(found) > self.offset + self.limit:
            result["truncated"] = True
            result["hint"] = f"Use offset={self.offset + self.limit} to see more results."
        if self.stop_reason is not None:
            result["truncated"] = True
            result["scan_truncated"] = True
            result["scan_stop_reason"] = self.stop_reason
            result.setdefault("hint", self._scan_hint(self.stop_reason))
        return result

    def _scan_hint(self, reason: _ScanStopReason) -> str:
        match reason:
            case "time_budget":
                return (
                    f"Scan stopped after the {TOOLS_TIMEOUTS['file_tools_search_time_budget_s']}s "
                    "time budget; results may be incomplete. Narrow the search path to see more."
                )
            case "max_matches":
                return (
                    f"Scan stopped at the {self.max_matches}-match scan limit; results may be "
                    "incomplete. Narrow the search pattern to see more."
                )


def bounded_walk(root: Path, state: ScanState) -> Iterator[tuple[Path, list[str]]]:
    """``os.walk`` pruned by :meth:`ScanState.prune` and cut on budget expiry."""
    for dirpath, dirnames, filenames in os.walk(root):
        state.prune(dirnames, Path(dirpath))
        if state.expired():
            state.note_budget()
            return
        yield Path(dirpath), filenames
