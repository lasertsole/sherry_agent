"""Shared head+tail clipping for TaskFlow prompt budgets.

One implementation, two consumers: the step judge's result view
(``step_judge._clip_for_judge``) and the ``aggregate_deps`` synthesis prompt
(``tools/_shared.build_task_with_dep_results``). Both need the END of a long
report — the verdict / the final implementation plan lives there — so the middle
is dropped behind a visible marker instead of keeping a head slice.

The clip is a pure string transform: callers own the budget and the wording of
the marker, so the judge's text stays byte-identical while the aggregate path
can say how much it dropped and where the full result still lives.
"""

from __future__ import annotations

__all__ = ["clip_text_head_tail"]

_DEFAULT_MARKER = "\n\n[... middle omitted ...]\n\n"


def clip_text_head_tail(text: str, limit: int, *, marker: str = _DEFAULT_MARKER) -> str:
    """Head 70% + ``marker`` + tail 30% (no-op when the text fits).

    ``limit <= 0`` disables clipping. When the marker alone would not fit the
    budget, the text degrades to a plain head slice: the caller asked for a hard
    bound, so the bound wins over keeping a tail.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    head = (limit * 7) // 10
    tail = limit - head - len(marker)
    if tail <= 0:
        return text[:limit]
    return text[:head] + marker + text[-tail:]
