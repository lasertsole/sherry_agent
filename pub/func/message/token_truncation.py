"""CJK-aware truncation by TOKEN budget instead of a raw character count.

A character cap means different things in different languages: 2 000 characters
is ~500 tokens of English but ~1 000 tokens of Chinese, so a Chinese transcript
paid double for the same "budget". Everything here works in the token currency
the estimator already uses (``estimate_text_tokens``: CJK ≈ 2 chars/token,
ASCII ≈ 4 chars/token), so a budget means the same thing in every language.

The old character constants map onto token budgets 1:1 for English
(``chars_per_token`` = 4), so an English session sees byte-identical behaviour;
a CJK session truncates earlier in characters, at the SAME token cost.
"""

from __future__ import annotations

from config.features import TOKEN_ESTIMATION
from pub.func.cjk import is_cjk_codepoint
from pub.func.estimate_tokens import estimate_text_tokens

__all__ = ["truncate_by_tokens"]

_CHARS_PER_TOKEN = TOKEN_ESTIMATION["chars_per_token"]
_CHARS_PER_TOKEN_CJK = TOKEN_ESTIMATION["chars_per_token_cjk"]


def _chars_for_tokens(text: str, target_tokens: float, *, from_end: bool = False) -> int:
    """How many characters of ``text`` cost ``target_tokens``, walking one end.

    Character-exact: a CJK character costs ``1 / chars_per_token_cjk`` and any
    other character ``1 / chars_per_token``, accumulated until the budget is
    reached. O(n) in the text length (these strings are already capped).
    """
    if target_tokens <= 0:
        return 0
    source = reversed(text) if from_end else text
    accumulated = 0.0
    count = 0
    for ch in source:
        accumulated += (
            1.0 / _CHARS_PER_TOKEN_CJK if is_cjk_codepoint(ord(ch)) else 1.0 / _CHARS_PER_TOKEN
        )
        count += 1
        if accumulated >= target_tokens:
            return count
    return count


def truncate_by_tokens(
    text: str,
    max_tokens: int,
    head_ratio: float = 0.6,
    tail_ratio: float = 0.4,
    *,
    omission_template: str = "...[truncated {omitted} chars]...",
) -> str:
    """Truncate ``text`` to a token budget, keeping a head and a tail slice.

    A text already inside the budget is returned unchanged (idempotent). The
    omission marker reports how many CHARACTERS were dropped, matching the
    long-standing wording so existing readers (and tests) see the same shape.
    """
    if max_tokens <= 0 or estimate_text_tokens(text) <= max_tokens:
        return text

    head_tokens = max_tokens * head_ratio
    tail_tokens = max_tokens * tail_ratio

    head_chars = _chars_for_tokens(text, head_tokens)
    tail_chars = _chars_for_tokens(text, tail_tokens, from_end=True)
    # Never overlap the two slices: a short-but-dense text could otherwise take
    # the same characters on both sides.
    tail_chars = min(tail_chars, max(0, len(text) - head_chars))

    head = text[:head_chars]
    tail = text[len(text) - tail_chars :] if tail_chars > 0 else ""
    omitted = len(text) - head_chars - tail_chars
    return f"{head}{omission_template.format(omitted=omitted)}{tail}"
