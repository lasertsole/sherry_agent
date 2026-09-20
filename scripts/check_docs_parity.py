#!/usr/bin/env python3
"""Four-language README structural parity gate.

Why this exists
---------------
The repository documents every major subsystem in four languages (``README.md``
plus ``README.{zh,ja,ko}.md``). Reviewers read one language; drift in the other
three — a missing section, a dropped code block, a table that lost rows — is
invisible until a user notices the translation no longer matches the source of
truth. This script turns that review burden into a deterministic check.

What is compared
----------------
For every discovered group the signature is:

  * heading level sequence — every line matching ``^(#{1,6}) `` outside fenced
    code blocks, reduced to its level. Heading *text* is translated, so only
    the level structure is comparable; levels 1-6 are all tracked so a
    deeper-heading divergence cannot hide.
  * fence count — every opening AND closing fence line (`` ``` `` / ``~~~``,
    with matching-length closing runs when nested longer fences are used).
    This catches both a missing code block and an unbalanced fence.
  * table row count — every line whose first non-space character is ``|``.
    Catches a table dropped or merged between translations.

Exit codes
----------
0 — every discovered group is structurally consistent.
1 — at least one mismatch, or no group was discovered (a discovery bug must
    not pass as success).

Usage
-----
    uv run --no-sync python scripts/check_docs_parity.py
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE = "README.md"
TRANSLATIONS = ("README.zh.md", "README.ja.md", "README.ko.md")

# Directories that never hold documentation groups worth gating: VCS/runtime
# state, dependency trees, generated output, and gitignored persona files.
SKIP_DIRS = frozenset(
    {
        ".codegraph",
        ".git",
        ".mypy_cache",
        ".nuxt",
        ".omo",
        ".playwright-mcp",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "dist",
        "logs",
        "node_modules",
        "src",
        "temp",
        "workspace",
    }
)

# Explicit exemptions: repo-relative group directory -> reason.
#
# Empty on purpose: every group in this repository can and does satisfy the
# structural contract (enforced by this gate). A group may only be listed here
# when its divergence is deliberate and language-specific — never to silence a
# drift that should be fixed. Adding an entry requires the reason to explain
# why fixing the documents instead is wrong.
ALLOWLIST: dict[str, str] = {}


@dataclass(frozen=True)
class Signature:
    """Structural fingerprint of one README."""

    headings: tuple[int, ...]
    fences: int
    table_rows: int


_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING = re.compile(r"^(#{1,6}) ")


def measure(path: Path) -> Signature:
    """Return the structural signature of the README at ``path``."""
    headings: list[int] = []
    fences = 0
    table_rows = 0
    in_fence = False
    fence_char = ""
    fence_len = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        fence = _FENCE.match(raw)
        if fence is not None:
            run = fence.group(1)
            if not in_fence:
                in_fence = True
                fence_char = run[0]
                fence_len = len(run)
            elif run[0] == fence_char and len(run) >= fence_len:
                in_fence = False
            fences += 1
            continue
        if in_fence:
            continue
        heading = _HEADING.match(raw)
        if heading is not None:
            headings.append(len(heading.group(1)))
        elif raw.lstrip().startswith("|"):
            table_rows += 1
    return Signature(tuple(headings), fences, table_rows)


def discover_groups(root: Path) -> list[Path]:
    """Return every directory holding all four README language variants."""
    groups: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        path = Path(dirpath)
        files = set(filenames)
        if REFERENCE in files and all(name in files for name in TRANSLATIONS):
            groups.append(path)
    return sorted(groups, key=lambda p: str(p.relative_to(root)))


def _first_divergence(reference: Signature, other: Signature) -> int | None:
    """Index of the first differing heading level, or ``None`` when equal."""
    limit = min(len(reference.headings), len(other.headings))
    for index in range(limit):
        if reference.headings[index] != other.headings[index]:
            return index
    return None if len(reference.headings) == len(other.headings) else limit


def _out(text: str) -> None:
    """Write one line to stdout (keeps ruff's flake8-print gate happy)."""
    sys.stdout.write(text + "\n")


def _format_signature(signature: Signature) -> str:
    return (
        f"headings={len(signature.headings)} "
        f"fences={signature.fences} "
        f"tables={signature.table_rows}"
    )


def main() -> int:
    """Run the parity gate; return the process exit code."""
    groups = discover_groups(REPO_ROOT)
    _out(f"README structural parity — {len(groups)} four-language group(s) discovered")
    if not groups:
        _out("FAIL: no four-language README group discovered under " + str(REPO_ROOT))
        return 1

    failures = 0
    allowlisted = 0
    for group in groups:
        name = str(group.relative_to(REPO_ROOT))
        reason = ALLOWLIST.get(name)
        if reason is not None:
            allowlisted += 1
            _out(f"SKIP {name} (allowlisted): {reason}")
            continue

        reference = measure(group / REFERENCE)
        mismatches: list[Signature] = []
        details: list[str] = []
        for translation in TRANSLATIONS:
            other = measure(group / translation)
            if other == reference:
                continue
            mismatches.append(other)
            drift = _first_divergence(reference, other)
            parts = []
            if len(other.headings) != len(reference.headings):
                parts.append(
                    f"headings {len(reference.headings)} vs {len(other.headings)}"
                    f" (first divergence at index {drift})"
                )
            if other.fences != reference.fences:
                parts.append(f"fences {reference.fences} vs {other.fences}")
            if other.table_rows != reference.table_rows:
                parts.append(f"tables {reference.table_rows} vs {other.table_rows}")
            details.append(f"      {translation}: " + "; ".join(parts))

        if not mismatches:
            _out(f"PASS {name} — {_format_signature(reference)}")
            continue

        failures += 1
        _out(f"FAIL {name}")
        _out(f"      {REFERENCE} (reference): {_format_signature(reference)}")
        for detail in details:
            _out(detail)

    checked = len(groups) - allowlisted
    _out(
        f"{len(groups)} group(s): {checked - failures} passed, "
        f"{failures} failed, {allowlisted} allowlisted"
    )
    _out("VERDICT: PASS" if failures == 0 else "VERDICT: FAIL")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
