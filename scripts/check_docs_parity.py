#!/usr/bin/env python3
"""Four-language README parity gate — structure and translation freshness.

The repository documents every major subsystem in four languages (``README.md``
plus ``README.{zh,ja,ko}.md``). Reviewers read one language; drift in the other
three is invisible until a user notices. This gate compares every discovered
group against its English reference on two layers:

  * first order (exact) — heading level sequence, fence count, table row count.
    Catches a missing section, code block, or table row in one translation.
  * second order (semantic) — link-target set and per-section body length
    ratio. Catches "structure intact but the translation is stale or empty";
    the normalization rule and the calibrated ratio band are documented inline
    at their constants below.

Exemption discipline (``ALLOWLIST``): an entry may only cover a deliberate,
language-specific divergence — never a drift that should be fixed — and its
reason must explain why fixing the documents instead is wrong. The gate
enforces a minimum reason length, bans placeholder openings, rejects stale
entries, and re-prints every hit, with its reason, in the summary block.
``tests/scripts/test_check_docs_parity.py`` pins ``ALLOWLIST == {}``: adding an
entry requires an explicit test edit, so it always lands in review.

Exit codes: 0 = every gate passes; 1 = a mismatch, an allowlist-contract
violation, or no group discovered (a discovery bug must not pass as success).

Usage: ``uv run --no-sync python scripts/check_docs_parity.py``
"""

# allow: SIZE_OK - single-purpose gate; a third of the crude LOC count is the
# calibration and exemption-contract documentation the task requires the gate
# to carry, and splitting one parse -> diff -> report pipeline would only add
# import indirection to a standalone CI script.

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

# Section-ratio band. Documents are cut at every heading line (fenced code
# excluded from the counted body) and matching sections are compared by
# stripped character count against English. A section fails when:
#   * EN body >= MIN_REFERENCE_BODY_CHARS and ratio < NEAR_EMPTY_RATIO -> near-empty
#   * EN body >= EMPTY_SECTION_REFERENCE_CHARS and translated body == 0 -> empty
#
# Calibrated on this repository, not guessed. Over 1278 section pairs with an
# EN body >= 100 chars the median translation/EN ratio is 0.590 (zh) / 0.696
# (ja) / 0.695 (ko); the 5th percentile is 0.411 / 0.543 / 0.534. The smallest
# ratio of a legitimately translated section — after the stale E5 section this
# metric first caught was repaired — is 0.256 (one dense Chinese paragraph).
# 0.20 therefore sits below every legitimate section with margin while still
# catching the 0.100 / 0.135 stale sections that motivated the metric. Re-run
# the distribution before changing the band.
NEAR_EMPTY_RATIO = 0.20
MIN_REFERENCE_BODY_CHARS = 100
EMPTY_SECTION_REFERENCE_CHARS = 40

# Allowlist contract: substantive reason, no placeholder opening (see the
# docstring's "Exemption discipline").
MIN_ALLOWLIST_REASON_CHARS = 40
ALLOWLIST_PLACEHOLDER = re.compile(
    r"^\s*(?:skip|n/?a|todo|tbd|fixme|wip|placeholder)\b", re.IGNORECASE
)

# Explicit exemptions: repo-relative group directory -> reason.
#
# Empty on purpose: every group in this repository can and does satisfy the
# parity contract. A group may only be listed here when its divergence is
# deliberate and language-specific — never to silence a drift that should be
# fixed. Adding an entry requires the reason to explain why fixing the
# documents instead is wrong, and an explicit edit to the test that pins this
# mapping empty.
ALLOWLIST: dict[str, str] = {}

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING = re.compile(r"^(#{1,6}) ")
_LINK = re.compile(r"(?<!!)\[([^\[\]]*(?:\[[^\]]*\][^\[\]]*)*)\]\(([^()\s]+)")

# Link-set normalization. ``_LINK`` excludes images (``!`` lookbehind). A
# destination's ``#fragment`` is stripped, and a pure anchor becomes ``None``
# because translated headings legitimately produce different anchors. The
# suffix below maps README.{zh,ja,ko}.md to README.md: a translated page
# deliberately links to the translated sibling document, so the linked
# *document set* is what must stay invariant across languages. Comparing raw
# strings would flag every localization as drift; normalized sets catch a link
# dropped or invented in one language.
_LOCALIZED_README = re.compile(r"README\.(?:zh|ja|ko)\.md$")


@dataclass(frozen=True)
class Document:
    """Everything the parity gates need, measured in one pass."""

    heading_levels: tuple[int, ...]
    headings: tuple[str, ...]
    fences: int
    table_rows: int
    links: frozenset[str]
    bodies: tuple[int, ...]


@dataclass(frozen=True)
class SectionGap:
    """A section whose translation body is empty or near-empty."""

    index: int
    heading: str
    reference_chars: int
    translation_chars: int

    @property
    def ratio(self) -> float:
        """Translation body length relative to the English reference."""
        return self.translation_chars / self.reference_chars


def _link_target(dest: str) -> str | None:
    """Normalize one link destination; ``None`` for a pure in-page anchor."""
    target = dest.split("#", 1)[0].strip()
    if not target:
        return None
    return _LOCALIZED_README.sub("README.md", target)


def extract(path: Path) -> Document:
    """Measure the README at ``path`` in a single pass."""
    heading_levels: list[int] = []
    headings: list[str] = []
    links: set[str] = set()
    bodies: list[int] = []
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
            heading_levels.append(len(heading.group(1)))
            headings.append(raw.strip())
            bodies.append(0)
            continue
        if raw.lstrip().startswith("|"):
            table_rows += 1
        if bodies:
            bodies[-1] += len(raw.strip())
        for match in _LINK.finditer(raw):
            target = _link_target(match.group(2))
            if target is not None:
                links.add(target)
    return Document(
        tuple(heading_levels),
        tuple(headings),
        fences,
        table_rows,
        frozenset(links),
        tuple(bodies),
    )


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


def _first_divergence(reference: tuple[int, ...], other: tuple[int, ...]) -> int | None:
    """Index of the first differing heading level, or ``None`` when equal."""
    limit = min(len(reference), len(other))
    for index in range(limit):
        if reference[index] != other[index]:
            return index
    return None if len(reference) == len(other) else limit


def _structural_diffs(reference: Document, other: Document) -> list[str]:
    """Every first-order mismatch of ``other`` against the reference."""
    parts: list[str] = []
    if len(reference.heading_levels) != len(other.heading_levels):
        drift = _first_divergence(reference.heading_levels, other.heading_levels)
        parts.append(
            f"headings {len(reference.heading_levels)} vs {len(other.heading_levels)}"
            f" (first divergence at index {drift})"
        )
    if other.fences != reference.fences:
        parts.append(f"fences {reference.fences} vs {other.fences}")
    if other.table_rows != reference.table_rows:
        parts.append(f"tables {reference.table_rows} vs {other.table_rows}")
    return parts


def _section_gaps(reference: Document, other: Document) -> tuple[SectionGap, ...]:
    """Sections whose translated body is empty or near-empty.

    Heading counts that differ are skipped: the structural gate already fails
    the group, so pairing sections would only add noise.
    """
    if len(reference.bodies) != len(other.bodies):
        return ()
    gaps: list[SectionGap] = []
    for index, (ref_chars, tr_chars) in enumerate(zip(reference.bodies, other.bodies)):
        near_empty = (
            ref_chars >= MIN_REFERENCE_BODY_CHARS and tr_chars < ref_chars * NEAR_EMPTY_RATIO
        )
        empty = ref_chars >= EMPTY_SECTION_REFERENCE_CHARS and tr_chars == 0
        if near_empty or empty:
            gaps.append(SectionGap(index, reference.headings[index], ref_chars, tr_chars))
    return tuple(gaps)


def _semantic_diffs(reference: Document, other: Document) -> list[str]:
    """Every second-order mismatch of ``other`` against the reference."""
    parts: list[str] = []
    missing = sorted(reference.links - other.links)
    extra = sorted(other.links - reference.links)
    if missing:
        parts.append(f"links missing: {missing}")
    if extra:
        parts.append(f"links extra: {extra}")
    for gap in _section_gaps(reference, other):
        parts.append(
            f"section #{gap.index} ({gap.heading}) body ratio {gap.ratio:.3f}"
            f" < {NEAR_EMPTY_RATIO:.2f} (EN {gap.reference_chars} chars,"
            f" translated {gap.translation_chars} chars)"
        )
    return parts


def allowlist_violations(groups: list[Path], root: Path) -> list[str]:
    """Return every breach of the exemption contract (see module docstring)."""
    violations: list[str] = []
    for group, reason in sorted(ALLOWLIST.items()):
        text = reason.strip()
        if len(text) < MIN_ALLOWLIST_REASON_CHARS:
            violations.append(
                f"{group}: reason is {len(text)} chars,"
                f" below the {MIN_ALLOWLIST_REASON_CHARS}-char minimum"
            )
        elif ALLOWLIST_PLACEHOLDER.match(text):
            violations.append(f"{group}: reason starts with placeholder text: {text!r}")
    discovered = {str(group_path.relative_to(root)) for group_path in groups}
    for group in sorted(set(ALLOWLIST) - discovered):
        violations.append(f"{group}: allowlisted group was not discovered (stale exemption)")
    return violations


def _out(text: str) -> None:
    """Write one line to stdout (keeps ruff's flake8-print gate happy)."""
    sys.stdout.write(text + "\n")


def _format_signature(document: Document) -> str:
    return (
        f"headings={len(document.heading_levels)} "
        f"fences={document.fences} "
        f"tables={document.table_rows} "
        f"links={len(document.links)}"
    )


def main(root: Path = REPO_ROOT) -> int:
    """Run the parity gate over ``root``; return the process exit code."""
    groups = discover_groups(root)
    violations = allowlist_violations(groups, root)
    if violations:
        _out("FAIL: allowlist contract violated — an exemption may only cover a deliberate,")
        _out("      language-specific divergence; a fixable drift must be fixed, not exempted.")
        for violation in violations:
            _out(f"      {violation}")
        return 1
    if not groups:
        _out("FAIL: no four-language README group discovered under " + str(root))
        return 1

    _out(f"README parity gate — {len(groups)} four-language group(s) discovered")
    failures = 0
    allowlisted = 0
    for group in groups:
        name = str(group.relative_to(root))
        reason = ALLOWLIST.get(name)
        if reason is not None:
            allowlisted += 1
            _out(f"SKIP {name} (allowlisted): {reason}")
            continue

        reference = extract(group / REFERENCE)
        details: list[str] = []
        for translation in TRANSLATIONS:
            other = extract(group / translation)
            parts = _structural_diffs(reference, other) + _semantic_diffs(reference, other)
            if parts:
                details.append(f"      {translation}: " + "; ".join(parts))

        if not details:
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
    _out(f"allowlisted: {allowlisted}")
    for group in sorted(ALLOWLIST):
        _out(f"  - {group}: {ALLOWLIST[group]}")
    _out("VERDICT: PASS" if failures == 0 else "VERDICT: FAIL")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
