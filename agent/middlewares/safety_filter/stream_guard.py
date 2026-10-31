"""Streaming text guard: scan BEFORE emitting, hold one window back.

The whole point of a live-stream filter is that blocked text never reaches the
viewer. The naive design (emit every delta, scan a side buffer) cannot do that:
whatever already went out stays out. This guard holds the last ``window``
characters back and only releases text it has already scanned.

Each release scans the WHOLE pending buffer and then decides how much may leave:

* a ``block`` match anywhere in the buffer latches the guard immediately — the
  buffered text is dropped, and the caller emits the replacement notice once;
* a ``replace`` match that ENDS inside the releasable prefix is substituted
  before the text leaves; one that would be cut in half by the release point
  holds the release back to its start (the match is retried next time);
* otherwise the prefix leaves untouched.

What this can guarantee, stated honestly: every match whose END is inside what
was emitted has been replaced or blocked first. A match longer than the window
can only complete in the tail, so it is caught by the end-of-stream flush — at
most ``len(match) - 1`` characters can be visible before the cut.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config.features import SAFETY_FILTER

from .rules import FilterHit, apply_replacements, scan

__all__ = ["StreamTextGuard"]

#: Hard stop for the replace loop: a rule whose replacement re-matches itself
#: must not spin (the text is released instead, once the cap is reached).
_MAX_REPLACEMENTS_PER_RELEASE = 50


@dataclass
class StreamTextGuard:
    """Hold-back window scanner for one streamed answer."""

    window: int = 0
    blocked: bool = False
    #: Hits found so far (block / replace / warn), for the caller's notice.
    hits: list[FilterHit] = field(default_factory=list)
    _pending: str = ""

    def __post_init__(self) -> None:
        if self.window <= 0:
            self.window = max(1, int(SAFETY_FILTER["stream_buffer_window"]))

    def feed(self, text: str) -> str:
        """Append a delta; return the prefix that is safe to emit ('' while holding)."""
        if self.blocked or not text:
            return ""
        self._pending += text
        return self._release()

    def flush(self) -> str:
        """End of stream: scan and return whatever is still held back."""
        if self.blocked or not self._pending:
            return ""
        buffer = self._pending
        self._pending = ""
        hits = scan(buffer, scope="output")
        if not hits:
            return buffer
        self.hits.extend(hits)
        if any(hit.action == "block" for hit in hits):
            self.blocked = True
            return ""
        return apply_replacements(buffer, hits)

    def _release(self) -> str:
        """Release the scanned prefix; return '' while the window is not full."""
        buffer = self._pending
        cut = len(buffer) - self.window
        if cut <= 0:
            return ""
        hits = scan(buffer, scope="output")
        if hits:
            self.hits.extend(hits)
            if any(hit.action == "block" for hit in hits):
                self.blocked = True
                self._pending = ""
                return ""
            buffer, cut = self._apply_replaces(buffer, cut)
        self._pending = buffer[cut:]
        return buffer[:cut]

    def _apply_replaces(self, buffer: str, cut: int) -> tuple[str, int]:
        """Substitute replace matches that end inside the prefix; hold the rest.

        Returns the (possibly rewritten) buffer and the new release point: a
        match cut in half by ``cut`` pulls the release back to its start so the
        next feed sees it whole.
        """
        for _ in range(_MAX_REPLACEMENTS_PER_RELEASE):
            replace_hits = [hit for hit in scan(buffer, scope="output") if hit.action == "replace"]
            if not replace_hits:
                return buffer, cut
            changed = False
            for hit in replace_hits:
                start = buffer.find(hit.matched_text)
                if start < 0:
                    continue
                end = start + len(hit.matched_text)
                if end <= cut:
                    replacement = hit.replacement or SAFETY_FILTER["safe_replacement_message"]
                    if hit.pattern in replacement:
                        # A rewriting rule would loop: release it untouched.
                        continue
                    buffer = buffer[:start] + replacement + buffer[end:]
                    cut += len(replacement) - (end - start)
                    changed = True
                    break
                if start < cut < end:
                    # The release point splits the match: hold from its start.
                    return buffer, start
            if not changed:
                return buffer, cut
        return buffer, cut
