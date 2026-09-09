"""Pure repetition-detection primitives for :class:`OutputRepetitionGuard`.

Contents:

* content fingerprinting (``normalize_for_hash`` / ``content_hash``)
* three internal-repetition sub-detectors (sentence/line, character-run,
  phrase-periodic)
* reasoning-text extraction (``additional_kwargs`` keys and inline
  ``<think>``-style tags)

No session state is touched here — detection only.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from langchain_core.messages import AIMessage

# Only the tail of a long content string is hashed, to keep the comparison
# cheap and stable even when the rest of the text changes.
_TAIL_CHARS = 500
# Default minimum consecutive identical non-whitespace characters (e.g. 8x
# ``a``) required to flag a character run as "repetitive".
_CHAR_RUN_MIN = 8
# Sentence/line delimiter set used by the segment-level sub-detector.  Because
# ``\\n`` is included, line-level repetition is covered by the same pass.
_SENTENCE_SPLIT = re.compile(r"[。.!?！？\n]+")
# ``additional_kwargs`` keys that reasoning providers use to carry explicit
# chain-of-thought / reasoning text alongside the visible content.
_REASONING_KEYS = ("reasoning_content", "reasoning", "reasoning_text")
# Inline chain-of-thought wrappers emitted by various reasoning models.
# <think> / <thinking> — DeepSeek-R1 & OpenAI-style; <reasoning> — Qwen
# local / GGUF variants. Order matters for extraction/strip determinism.
_THINK_PATTERNS = [
    re.compile(r"<think>(.*?)</think>", re.DOTALL),
    re.compile(r"<thinking>(.*?)</thinking>", re.DOTALL),
    re.compile(r"<reasoning>(.*?)</reasoning>", re.DOTALL),
]


def normalize_for_hash(content: str) -> str:
    """Normalize content for robust cross-call hash comparison.

    Applies three transformations so that near-identical outputs
    (e.g. ``"Okay, I'll handle it."``, ``"Okay I'll handle it"``, ``"Okay, I'll handle it"``)
    all produce the same hash:

    1. **NFKC normalization** — full-width → half-width (``Ａ`` → ``A``)
    2. **Whitespace removal** — all spaces, tabs, newlines stripped
    3. **Punctuation removal** — all non-word characters removed

    The resulting string contains only letters (incl. CJK), digits and
    underscores, making the hash insensitive to formatting noise that
    shouldn't prevent repetition detection.
    """
    content = unicodedata.normalize("NFKC", content)
    content = re.sub(r"\s+", "", content)
    content = re.sub(r"[^\w]", "", content)
    return content


def content_hash(content: str) -> str:
    """Hash content for cross-call comparison.

    Returns a dual ``"head_hash|tail_hash"`` string:

    * For short content (≤ ``_TAIL_CHARS`` after normalization): both
      parts are the MD5 of the full normalized content.
    * For long content: head = MD5 of the first ``_TAIL_CHARS`` chars of
      the normalized content, tail = MD5 of the last ``_TAIL_CHARS``.

    This dual-hash approach catches repetition at **either** end of the
    output:

    * Same output → both match.
    * Same prefix, different suffix → head matches.
    * Different prefix, same suffix → tail matches.

    The caller should split on ``"|"`` and compare either part.
    """
    normalized = normalize_for_hash(content)
    if not normalized:
        return "|"

    if len(normalized) <= _TAIL_CHARS:
        h = hashlib.md5(normalized.encode()).hexdigest()
        return f"{h}|{h}"

    head = normalized[:_TAIL_CHARS]
    tail = normalized[-_TAIL_CHARS:]
    return f"{hashlib.md5(head.encode()).hexdigest()}|{hashlib.md5(tail.encode()).hexdigest()}"


class SentenceRepetitionDetector:
    """Segment-level sub-detector: duplicate ratio of sentences/lines.

    Splits ``content`` on sentence-/line-ending punctuation (and ``\\n``,
    so this doubles as the line detector), then computes
    ``1 - (unique / total)``.  Returns ``True`` if more than
    ``internal_repeat_ratio`` of the segments are duplicates and there are
    at least ``internal_min_lines`` of them (to avoid short-response false
    positives).
    """

    def __init__(self, internal_repeat_ratio: float = 0.6, internal_min_lines: int = 6):
        self.internal_repeat_ratio = internal_repeat_ratio
        self.internal_min_lines = internal_min_lines

    def detect(self, content: str) -> bool:
        sentences = [s.strip() for s in _SENTENCE_SPLIT.split(content) if s.strip()]
        if len(sentences) < self.internal_min_lines:
            return False
        unique = len(set(sentences))
        ratio = 1.0 - (unique / len(sentences))
        return ratio > self.internal_repeat_ratio


class CharRunDetector:
    """Character-run sub-detector: same char repeated consecutively.

    Uses a regex back-reference ``\\1`` to find ``char_run_min`` or more
    consecutive identical non-whitespace characters (e.g. ``aaaa…``),
    which is a strong signal of stuttering/death-loop output.
    """

    def __init__(self, char_run_min: int = _CHAR_RUN_MIN):
        self.char_run_min = char_run_min

    def detect(self, content: str) -> bool:
        threshold = self.char_run_min - 1
        pattern = rf"([^\s])\1{{{threshold},}}"
        return bool(re.search(pattern, content))


class PhraseRepetitionDetector:
    """Phrase-periodic sub-detector: short substring repeated back-to-back.

    Looks for any phrase of length ``2..max_phrase`` that repeats
    immediately and contiguously ``min_repeats``+ times with no
    delimiter -- e.g. ``I can helpI can helpI can helpI can helpI can helpI can help``.
    Whitespace is stripped first so delimiters do not mask the pattern.
    """

    def detect(self, content: str, min_repeats: int = 5, max_phrase: int = 10) -> bool:
        content = re.sub(r"[ \t]+", "", content)
        n = len(content)
        if n < 2 * min_repeats:
            return False

        # A phrase long enough to repeat ``min_repeats`` times cannot exceed
        # ``n // min_repeats`` chars -- this bounds the search space.
        upper = min(max_phrase, n // min_repeats)
        for plen in range(2, upper + 1):
            # Try every feasible start offset so the pattern is not required
            # to begin at index 0 (the string may have a non-looping prefix).
            limit = n - plen * min_repeats
            for start in range(min(plen, limit + 1)):
                pattern = content[start : start + plen]
                if not pattern.strip():
                    continue
                # Greedy contiguous repetition count from this offset.
                repeats = 1
                pos = start + plen
                while pos + plen <= n and content[pos : pos + plen] == pattern:
                    repeats += 1
                    pos += plen
                if repeats >= min_repeats:
                    return True
        return False


def extract_reasoning(msg: AIMessage) -> str:
    """Extract explicit reasoning text stored on ``additional_kwargs``.

    Checks the keys in :data:`_REASONING_KEYS` in order and returns the
    first non-empty value, stripped.
    """
    ak = getattr(msg, "additional_kwargs", None)
    if ak and isinstance(ak, dict):
        for key in _REASONING_KEYS:
            reasoning = str(ak.get(key, "") or "").strip()
            if reasoning:
                return reasoning
    return ""


def extract_inline_reasoning(content: str) -> str:
    """Extract reasoning text wrapped in inline CoT tags (``<think>`` etc.).

    Concatenates the captured body of every matching tag (in ``_THINK_PATTERNS``
    order) separated by newlines.  Used as a fallback when no explicit
    ``additional_kwargs`` reasoning is available.
    """
    parts = [
        m.group(1).strip()
        for pattern in _THINK_PATTERNS
        for m in pattern.finditer(content)
        if m.group(1).strip()
    ]
    return "\n".join(parts)


def strip_inline_reasoning(content: str) -> str:
    """Remove inline CoT tags (``<think>`` etc.) from visible content.

    The complement of :func:`extract_inline_reasoning` -- once reasoning
    has been extracted, the tags are removed from the visible output so the
    repetition detector operates on the cleaned text.  Returns the stripped,
    trimmed remainder.
    """
    for pattern in _THINK_PATTERNS:
        content = pattern.sub("", content)
    return content.strip()
