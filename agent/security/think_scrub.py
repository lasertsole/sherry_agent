"""Move inline chain-of-thought from the answer channel to the reasoning one (B4).

Reasoning models emit their thinking either in a separate ``additional_kwargs``
key (this repository's normal path) or inline in ``content``, wrapped in
``<think>`` / ``<thinking>`` / ``<reasoning>`` tags. The inline form reaches the
client as ordinary answer text — the reasoning bubble never sees it, and the
persisted answer keeps it — so it has to be moved on the way out.

Moved, not dropped: the frontend renders ``{"type": "reasoning"}`` chunks as a
collapsible thinking block (``ChatThinkingBlock.vue``), so discarding the text
would fix the leak by breaking the feature for exactly the models that use inline
tags. :meth:`StreamingReasoningScrubber.feed` returns the visible answer text and
:meth:`take_reasoning` the text recovered from inside the tags, which the caller
emits on the reasoning channel.

A whole-message regex is not enough: content arrives in chunks, and a tag can be
split anywhere (``"<thi"`` then ``"nk>…</thin"`` then ``"k>"``). The scrubber
therefore keeps a small amount of state:

* text is emitted as soon as it is provably not part of a tag;
* a trailing prefix that *could* still become a tag is held back until more
  arrives (``"<thi"`` waits; ``"<x"`` does not, because no tag starts that way);
* inside a block the text is recovered into the reasoning buffer until the
  matching close tag;
* :meth:`flush` ends the stream: an unterminated block's recovered text stays
  available through :meth:`take_reasoning` (it was streamed, so the reader keeps
  it), while a held-back fragment that never became a tag is returned as the
  plain answer text it was.

The tag set matches the one the repetition guard extracts with (see
``agent/middlewares/output_repetition_guard``), so the two agree on what the
reasoning stream is. The per-turn lifetime matters: one scrubber per turn lives
on the turn object, never on a shared model instance, because tag state that
leaked between concurrent streams would corrupt both.
"""

from __future__ import annotations

__all__ = ["INLINE_REASONING_TAGS", "StreamingReasoningScrubber", "strip_inline_reasoning"]

#: Inline chain-of-thought wrappers seen in the wild: DeepSeek-R1 / OpenAI-style
#: ``<think[ing]>`` and Qwen's ``<reasoning>``.
INLINE_REASONING_TAGS: tuple[str, ...] = ("think", "thinking", "reasoning")


class StreamingReasoningScrubber:
    """Feed content chunks; split each into answer text and recovered reasoning."""

    def __init__(self) -> None:
        self._held = ""
        self._inside: str | None = None
        self._recovered: list[str] = []

    def feed(self, chunk: str) -> str:
        """Return the visible answer text carried by ``chunk`` (possibly empty).

        Reasoning recovered from inside a block is buffered; drain it with
        :meth:`take_reasoning`.
        """
        if not chunk:
            return ""
        self._held += chunk
        out: list[str] = []
        while self._held:
            if self._inside is not None:
                close = f"</{self._inside}>"
                index = self._held.lower().find(close)
                if index >= 0:
                    self._recovered.append(self._held[:index])
                    self._held = self._held[index + len(close) :]
                    self._inside = None
                    continue
                # Keep only what could still be the start of the close tag; the
                # rest is reasoning the reader never saw as answer text.
                held = _longest_partial_suffix(self._held, close)
                self._recovered.append(
                    self._held[: len(self._held) - len(held)] if held else self._held
                )
                self._held = held
                return "".join(out)

            opening = _next_opening_tag(self._held)
            if opening is None:
                # No tag here; emit everything except a suffix that could still
                # begin one (emit the TEXT, not the remainder we keep back).
                candidates = [f"<{tag}>" for tag in INLINE_REASONING_TAGS]
                held = _longest_partial_suffix(self._held, *candidates)
                out.append(self._held[: len(self._held) - len(held)] if held else self._held)
                self._held = held
                return "".join(out)

            index, tag = opening
            out.append(self._held[:index])
            self._held = self._held[index + len(tag) :]
            self._inside = tag[1:-1]

        return "".join(out)

    def take_reasoning(self) -> str:
        """Drain the reasoning text recovered since the last call."""
        if not self._recovered:
            return ""
        text = "".join(self._recovered)
        self._recovered = []
        return text

    def flush(self) -> str:
        """End the stream: return answer text still owed to the reader."""
        if self._inside is not None:
            # An unterminated block: its content was never answer text, but it WAS
            # streamed, so it stays available as reasoning instead of vanishing.
            self._recovered.append(self._held)
            self._held = ""
            self._inside = None
            return ""
        held, self._held = self._held, ""
        return held


def _longest_partial_suffix(text: str, *candidates: str) -> str:
    """The longest suffix of ``text`` that is a proper prefix of a candidate."""
    lowered = text.lower()
    for size in range(min(len(text), max(len(c) for c in candidates) - 1), 0, -1):
        suffix = lowered[-size:]
        if any(candidate.lower().startswith(suffix) for candidate in candidates):
            return text[-size:]
    return ""


def _next_opening_tag(text: str) -> tuple[int, str] | None:
    """The earliest complete opening tag in ``text`` as ``(index, tag)``."""
    lowered = text.lower()
    best: tuple[int, str] | None = None
    for tag in INLINE_REASONING_TAGS:
        opening = f"<{tag}>"
        index = lowered.find(opening)
        if index >= 0 and (best is None or index < best[0]):
            best = (index, opening)
    return best


def strip_inline_reasoning(text: str) -> str:
    """Whole-text convenience wrapper (non-streaming callers and tests)."""
    scrubber = StreamingReasoningScrubber()
    return scrubber.feed(text) + scrubber.flush()
