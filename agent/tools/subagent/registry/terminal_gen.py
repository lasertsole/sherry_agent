"""Terminal generation tracker: gates completion callbacks by expected generation to prevent stale callbacks.

Tracks expected generation per run → guards completion callbacks → self-invalidates older generations → cleans up on completion.
"""

from collections.abc import Callable


class TerminalGenerationTracker:
    """Tracks the expected generation for each run to gate stale completion callbacks."""

    def __init__(self):
        self._expected: dict[str, int] = {}

    def register_expected(self, run_id: str, generation: int) -> None:
        """Register the expected generation for a run."""
        self._expected[run_id] = generation

    def is_callback_current(self, run_id: str, generation: int) -> bool:
        """Return True if the given generation is current (>= expected) for the run."""
        expected = self._expected.get(run_id)
        if expected is None:
            return True
        return generation >= expected

    def is_older_equivalent(self, run_id: str, generation: int) -> bool:
        """Return True if the given generation is strictly older than the expected one."""
        expected = self._expected.get(run_id)
        if expected is None:
            return False
        return generation < expected

    def retire(self, run_id: str) -> None:
        """Remove the expected generation entry after the run completes."""
        self._expected.pop(run_id, None)

    def prune_missing(self, exists: "Callable[[str], bool]") -> int:
        """Drop entries whose run is no longer in the registry; returns pruned count.

        A run deleted by orphan cleanup never reaches ``retire``, so without this
        sweep the tracker retains one dead run id per such run.
        """
        removed = 0
        for run_id in list(self._expected):
            if not exists(run_id):
                self._expected.pop(run_id, None)
                removed += 1
        return removed


_terminal_gen_tracker = TerminalGenerationTracker()


def get_terminal_gen_tracker() -> TerminalGenerationTracker:
    """Return the singleton TerminalGenerationTracker instance."""
    return _terminal_gen_tracker
