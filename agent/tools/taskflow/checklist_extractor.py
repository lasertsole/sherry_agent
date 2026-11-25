"""Extract an acceptance checklist from a reviewer task's normative clauses.

Reviewers spawned for a TaskFlow step otherwise get the raw task text and write
free-form prose. Turning explicit normative sentences ("must ...", "raise ...",
"reject ...") into a numbered PASS/FAIL checklist makes the self-check answerable
clause by clause instead of "read the plan and agree with it" (measured: a run
passed its verify while missing an explicitly stated rule).

Rules of thumb:
- normative markers follow the eval-side clause set (must/should/shall/raise/
  reject/only/never/charset/return/error ...); one clause per sentence;
- de-duplicated, capped at 20;
- empty result means "nothing to add" — the caller leaves the task untouched.
"""

from __future__ import annotations

import re

#: Normative markers, mirroring the eval-side clause set. Anchored to word
#: boundaries so "mustard"/"erroring" never trigger a false positive.
_NORMATIVE_PATTERNS = (
    re.compile(r"\bmust\b", re.IGNORECASE),
    re.compile(r"\bmust not\b", re.IGNORECASE),
    re.compile(r"\bshould\b", re.IGNORECASE),
    re.compile(r"\bshall\b", re.IGNORECASE),
    re.compile(r"\braise\b", re.IGNORECASE),
    re.compile(r"\breject\b", re.IGNORECASE),
    re.compile(r"\baccept\b", re.IGNORECASE),
    re.compile(r"\bonly\b", re.IGNORECASE),
    re.compile(r"\bnever\b", re.IGNORECASE),
    re.compile(r"\balways\b", re.IGNORECASE),
    re.compile(r"\bcharset\b", re.IGNORECASE),
    re.compile(r"\breturn\b", re.IGNORECASE),
    re.compile(r"\berror\b", re.IGNORECASE),
    re.compile(r"\bencoding\b", re.IGNORECASE),
)

#: Sentence boundaries: periods, newlines, semicolons and full-width stops.
_SENTENCE_SPLIT = re.compile(r"[.\n;。；]")
_MAX_CLAUSES = 20


def extract_acceptance_checklist(task_text: str) -> list[str]:
    """Return numbered-able normative clauses found in ``task_text``.

    Each returned string is a trimmed sentence that contains at least one strong
    normative marker. Order follows the text; duplicates are removed. An empty
    list (blank input, no normative sentence) means the caller should NOT append
    a checklist.
    """
    if not task_text or not task_text.strip():
        return []

    clauses: list[str] = []
    seen: set[str] = set()
    for raw in _SENTENCE_SPLIT.split(task_text):
        sentence = raw.strip()
        if len(sentence) < 5:
            continue
        if not any(pattern.search(sentence) for pattern in _NORMATIVE_PATTERNS):
            continue
        key = sentence.lower()
        if key in seen:
            continue
        seen.add(key)
        clauses.append(sentence)
        if len(clauses) >= _MAX_CLAUSES:
            break
    return clauses


#: Marker the eval side checks to avoid appending a second checklist.
ACCEPTANCE_CHECKLIST_LABEL = "ACCEPTANCE CHECKLIST"


def augment_task_with_checklist(task_text: str, clauses: list[str] | tuple[str, ...]) -> str:
    """Append a numbered PASS/FAIL checklist to ``task_text``.

    Idempotent: if a checklist is already present, return the text unchanged.
    """
    if not clauses:
        return task_text
    if ACCEPTANCE_CHECKLIST_LABEL in task_text:
        return task_text
    lines = "\n".join(f"{i}. {clause}" for i, clause in enumerate(clauses, 1))
    return (
        task_text.rstrip()
        + "\n\n"
        + ACCEPTANCE_CHECKLIST_LABEL
        + " — verify each clause, answer PASS or FAIL:\n"
        + lines
    )
