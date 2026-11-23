"""Deterministic acceptance-clause extraction shared by coding benchmarks.

Turns an issue/task description into a short list of binary, answerable
clauses. Extracted from ``evals/deepswe/suite.py`` so devbench and
multi-swe-bench install the same reviewer checklist instead of leaving their
reviewers to "read the plan and agree with it".
"""

from __future__ import annotations

import re

#: Normative language a reviewer can answer pass/fail.
_CLAUSE_KEYWORDS = (
    "must",
    "raise",
    "reject",
    "only",
    "return",
    "yield",
    "parse",
    "decode",
    "encode",
    "support",
    "require",
    "case-insensitive",
    "allow",
    "ignore",
)


def acceptance_checklist(instruction: str, *, limit: int = 8) -> list[str]:
    """Return the task text's decidable clauses as reviewer checklist items.

    Deterministic sentence/semicolon split plus a normative-keyword filter:
    the reviewer gets concrete, answerable lines instead of the whole essay.
    An empty result means the text carries no normative clauses; callers may
    then leave the checklist unset (= current behaviour).
    """
    text = re.sub(r"\s+", " ", instruction or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        chunks.extend(part.strip() for part in sentence.split(";") if part.strip())
    items = [
        chunk
        for chunk in chunks
        if len(chunk) >= 20 and any(word in chunk.lower() for word in _CLAUSE_KEYWORDS)
    ]
    return items[:limit]
