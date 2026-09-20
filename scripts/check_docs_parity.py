#!/usr/bin/env python3
"""Four-language README parity gate — structure and translation freshness.

The repository documents every major subsystem in four languages (``README.md``
plus ``README.{zh,ja,ko}.md``). Reviewers read one language; drift in the other
three is invisible until a user notices. This gate compares every discovered
group against its English reference on two layers:

  * first order (exact) — heading level sequence, fence count, table row count.
    Catches a missing section, code block, or table row in one translation.
  * second order (semantic) — link-target set with its per-target language
    variant set, per-section body length ratio, a non-empty check for even the
    smallest English sections, a language-independent heading-marker sequence,
    and a per-section invariant-token multiset. Together they catch "structure
    intact but the translation is stale": an emptied section (including a tiny
    one below the ratio band), a link retargeted at the wrong language, a
    reordered set of headings, and a section that is long enough yet has lost
    the code spans / file paths the English reference still carries. The
    normalization rules and the calibrated bands are documented inline at their
    constants below.
  * third order (markers) — every heading is reduced to the markers a
    translation cannot change (its inline code spans and link targets); the
    per-document marker sequence of each translation must equal the English
    one. An order mismatch is direct evidence a heading was shuffled even
    though the heading level sequence still matches; section pairing then
    prefers marker fingerprints over raw index, so the token comparison below
    is not mis-paired by such a shuffle.

Exemption discipline (``ALLOWLIST``): an entry may only cover a deliberate,
language-specific divergence — never a drift that should be fixed — and its
reason must both be substantive and contain an explicit "why fixing the
document(s) instead is wrong" clause (one of the templates in
``_ALLOWLIST_FIX_CLAUSE``) with a real rationale after it. The gate enforces a
minimum reason length, bans placeholder openings, requires that clause, rejects
stale entries, and re-prints every hit, with its reason, in the summary block.
``tests/scripts/test_check_docs_parity.py`` pins ``ALLOWLIST == {}``: adding an
entry requires an explicit test edit, so it always lands in review.

Recalibration: the ratio band is a snapshot, not a constant of nature. Run
``uv run --no-sync python scripts/check_docs_parity.py --calibrate`` to print the
per-language section-ratio distribution (min / p1 / p5 / median, over sections
with an English body >= ``MIN_REFERENCE_BODY_CHARS``) and the margin the smallest
legitimate ratio keeps above the band. The test file asserts that margin stays
above ``MIN_CALIBRATION_MARGIN``, so a newly added dense legitimate section
turns the test red before the band silently narrows. To retune a band: run
``--calibrate``, inspect the distribution, adjust the constant, update the
numbers in the comment at that constant, then re-run the gate and the test file.
``--calibrate`` never gates: it prints and exits 0; the default mode validates
and returns 0/1.

Exit codes: 0 = every gate passes; 1 = a mismatch, an allowlist-contract
violation, or no group discovered (a discovery bug must not pass as success).

Usage: ``uv run --no-sync python scripts/check_docs_parity.py``
       ``uv run --no-sync python scripts/check_docs_parity.py --calibrate``
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
#   * EN body < EMPTY_SECTION_REFERENCE_CHARS, EN has real (non-separator)
#     content, and the translation has none -> tiny empty (_tiny_empty_sections)
#
# Calibrated on this repository with ``--calibrate``, not guessed. Over 1278
# section pairs (3 translations x 426 sections) with an EN body >= 100 chars the
# per-language translation/EN ratio distribution is:
#   zh: min 0.256, p1 0.350, p5 0.414, median 0.591, margin 1.281x
#   ja: min 0.350, p1 0.452, p5 0.551, median 0.696, margin 1.750x
#   ko: min 0.350, p1 0.481, p5 0.548, median 0.696, margin 1.750x
# The smallest ratio of a legitimately translated section — after the stale E5
# section this metric first caught was repaired — is 0.256 (one dense Chinese
# paragraph). 0.20 therefore sits below every legitimate section with margin
# while still catching the 0.100 / 0.135 stale sections that motivated the
# metric. MIN_CALIBRATION_MARGIN pins the smallest legitimate ratio at >=1.15x
# the band so a denser legitimate section turns the test red before the band
# silently narrows; re-run --calibrate before changing the band.
NEAR_EMPTY_RATIO = 0.20
MIN_REFERENCE_BODY_CHARS = 100
EMPTY_SECTION_REFERENCE_CHARS = 40
MIN_CALIBRATION_MARGIN = 1.15

# Heading markers (blind spot B). A heading's translation-invariant content is
# its inline code spans and link targets; the heading prose around them can be
# rewritten freely. Markers are canonicalized as a SORTED tuple because a
# translation may reorder the code spans *inside* one heading to match its own
# word order (en "singleton `cron` in `core.py`" -> zh "`core.py` 中的 `cron`
# 单例") without that being a heading reorder. The document-level sequence of
# fingerprint-bearing headings must then match across languages; the first
# position where it does not is direct evidence a heading was shuffled even
# though the heading level sequence still passes. Marker-less headings carry no
# language-independent evidence and are paired by occurrence index instead (the
# fallback count is reported, and reordering among them cannot be detected).
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_LOCALIZED_README_ANY = re.compile(r"README\.(?:zh|ja|ko)\.md")
_PATH_TOKEN_TRAILING = ".,;:)]}"

# Invariant-token band (blind spot A). Per paired section we extract the tokens
# a translation must not lose: inline code spans, file paths, ENV-style
# identifiers, and numeric literals. A token counts as retained when its
# whitespace-stripped form appears anywhere in the translated section — inside a
# code span or as plain prose — so dropping the backticks is not treated as
# drift. Path placeholders (``<...>``) are matched literally; the calibration
# below shows the tail that survives that rule is small.
#
# Calibrated on this repository (1239 token-bearing section pairs = 3
# translations x 21 groups), counting missing code-span/path tokens per section:
#   0 missing:  1139 sections
#   1 missing:    80 sections — mostly legitimate: localized placeholder text
#                 (``--tmpfs<path>`` -> ``--tmpfs<路径>``), full-width
#                 punctuation, or an expression restated in prose.
#   >=2 missing:  20 sections — every one traced to a real content omission and
#                 repaired (a stale paragraph, a dropped bullet, a dropped file
#                 line); the smallest single-token ratio among them is 0.07, so
#                 a ratio band cannot separate the real omissions from the
#                 single-token tail.
# The gate therefore fails at >=2 missing code-span/path tokens, and also on a
# section that retains NONE of its reference code-span/path tokens (the
# "translation has no code span / path at all" case). The single-token tail is
# the documented residual limitation: a section that drops exactly one invariant
# token sits below the band and is not flagged. ENV/number tokens are extracted
# and reported for context, but do not drive the verdict — the observed
# weak-only tail is at most 2 per section and is dominated by notation drift.
MIN_MISSING_INVARIANT_TOKENS = 2
_FILE_PATH_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\."
    r"(?:py|pyi|md|toml|json|jsonc|ya?ml|sh|ts|tsx|js|mjs|vue|rs|go|cfg|ini|txt|html|css|db|sqlite)"
    r"(?![A-Za-z0-9_])"
)
_ENV_TOKEN = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9_]{3,}(?![A-Za-z0-9_])")
_NUMBER_TOKEN = re.compile(r"(?<![A-Za-z0-9_.])(?:\d+\.\d+|\d[\d_]*\d)")

# Allowlist contract: substantive reason, no placeholder opening, and an explicit
# "why fixing the documents instead is wrong" clause (see the docstring's
# "Exemption discipline"). The clause templates below are the only accepted
# openings; anything after them must be a real rationale of at least
# MIN_ALLOWLIST_FIX_RATIONALE_CHARS chars, so an exemption must argue, not just
# reach a character count.
MIN_ALLOWLIST_REASON_CHARS = 40
MIN_ALLOWLIST_FIX_RATIONALE_CHARS = 20
ALLOWLIST_PLACEHOLDER = re.compile(
    r"^\s*(?:skip|n/?a|todo|tbd|fixme|wip|placeholder)\b", re.IGNORECASE
)
_ALLOWLIST_FIX_CLAUSE = re.compile(
    r"(?:Fixing the documents? is wrong because"
    r"|Cannot be fixed in the documents? because"
    r"|The documents? cannot be fixed because)"
    r"\s+(?P<why>.+)$",
    re.IGNORECASE | re.DOTALL,
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
# dropped or invented in one language — but on their own they cannot see a link
# retargeted at the *wrong* language, because README.ja.md and README.md both
# collapse to README.md. Each document therefore also records, per normalized
# target, the set of language suffixes actually linked ("" for the base file).
# The gate then compares the "foreign" suffixes of EN and each translation after
# removing that translation's own language and the base: linking the base and
# linking one's own localized sibling are the same self-reference, so the
# language switcher (every page links the other three) stays equal, while a
# translation that links a foreign sibling EN does not link is flagged.
_OWN_SUFFIX = {"README.zh.md": "zh", "README.ja.md": "ja", "README.ko.md": "ko"}
_LOCALIZED_README = re.compile(r"README\.(zh|ja|ko)\.md$")


@dataclass(frozen=True)
class Document:
    """Everything the parity gates need, measured in one pass."""

    heading_levels: tuple[int, ...]
    headings: tuple[str, ...]
    fences: int
    table_rows: int
    links: frozenset[str]
    link_suffixes: tuple[tuple[str, tuple[str, ...]], ...]
    bodies: tuple[int, ...]
    heading_markers: tuple[tuple[str, ...], ...]
    section_texts: tuple[str, ...]


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


@dataclass(frozen=True)
class LanguageCalibration:
    """Per-language ratio distribution over sections above the ratio band."""

    language: str
    pairs: int
    minimum: float
    p1: float
    p5: float
    median: float

    @property
    def margin(self) -> float:
        """How many times the band the smallest legitimate ratio keeps."""
        return self.minimum / NEAR_EMPTY_RATIO


def _split_localized(target: str) -> tuple[str, str | None]:
    """Return ``(normalized base, language suffix or None)`` for one target."""
    match = _LOCALIZED_README.search(target)
    if match is None:
        return target, None
    return _LOCALIZED_README.sub("README.md", target), match.group(1)


def _link_target(dest: str) -> str | None:
    """Normalize one link destination; ``None`` for a pure in-page anchor."""
    target = dest.split("#", 1)[0].strip()
    if not target:
        return None
    return _split_localized(target)[0]


def _canon_marker(text: str) -> str:
    """Canonical form of a code-span/token body: whitespace is not meaningful."""
    return re.sub(r"\s+", "", text.strip())


def _heading_markers(heading: str) -> tuple[str, ...]:
    """Translation-invariant heading fingerprint (sorted inline code + links)."""
    markers = ["code:" + _canon_marker(match.group(1)) for match in _CODE_SPAN.finditer(heading)]
    for match in _LINK.finditer(heading):
        target = _link_target(match.group(2))
        if target is not None:
            markers.append("link:" + target)
    return tuple(sorted(markers))


def _invariant_tokens(text: str) -> frozenset[str]:
    """Tokens a translation must not lose, class-prefixed for reporting."""
    text = _LOCALIZED_README_ANY.sub("README.md", text)
    tokens: set[str] = set()
    for match in _CODE_SPAN.finditer(text):
        tokens.add("code:" + _canon_marker(match.group(1)))
    stripped = _CODE_SPAN.sub(" ", text)
    for match in _FILE_PATH_TOKEN.finditer(stripped):
        tokens.add("path:" + match.group(0).strip().rstrip(_PATH_TOKEN_TRAILING))
    for match in _ENV_TOKEN.finditer(stripped):
        token = match.group(0)
        if "_" in token or any(character.isdigit() for character in token):
            tokens.add("env:" + token)
    for match in _NUMBER_TOKEN.finditer(stripped):
        token = match.group(0)
        if "." in token or "_" in token or len(token) >= 3:
            tokens.add("num:" + token)
    return frozenset(tokens)


def _token_haystack(text: str) -> str:
    """Translated text flattened for substring retention checks."""
    text = _LOCALIZED_README_ANY.sub("README.md", text)
    text = _CODE_SPAN.sub(lambda match: _canon_marker(match.group(1)), text)
    return re.sub(r"\s+", "", text)


def _is_strong_token(token: str) -> bool:
    return token.startswith(("code:", "path:"))


def _token_label(token: str) -> str:
    return "`" + token.split(":", 1)[1] + "`"


def _missing_tokens(reference_tokens: frozenset[str], translated_text: str) -> frozenset[str]:
    """Reference tokens whose canonical body is absent from the translation."""
    haystack = _token_haystack(translated_text)
    return frozenset(
        token for token in reference_tokens if _canon_marker(token.split(":", 1)[1]) not in haystack
    )


def extract(path: Path) -> Document:
    """Measure the README at ``path`` in a single pass."""
    heading_levels: list[int] = []
    headings: list[str] = []
    links: set[str] = set()
    link_suffixes: dict[str, set[str]] = {}
    bodies: list[int] = []
    heading_markers: list[tuple[str, ...]] = []
    section_texts: list[str] = []
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
            heading_markers.append(_heading_markers(raw.strip()))
            section_texts.append("")
            continue
        if raw.lstrip().startswith("|"):
            table_rows += 1
        if bodies:
            bodies[-1] += len(raw.strip())
            section_texts[-1] += raw + "\n"
        for match in _LINK.finditer(raw):
            dest = match.group(2).split("#", 1)[0].strip()
            if not dest:
                continue
            target, suffix = _split_localized(dest)
            links.add(target)
            link_suffixes.setdefault(target, set()).add(suffix or "")
    return Document(
        tuple(heading_levels),
        tuple(headings),
        fences,
        table_rows,
        frozenset(links),
        tuple((base, tuple(sorted(suffixes))) for base, suffixes in sorted(link_suffixes.items())),
        tuple(bodies),
        tuple(heading_markers),
        tuple(section_texts),
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


def _format_marker(fingerprint: tuple[str, ...]) -> str:
    return " | ".join(fingerprint) if fingerprint else "<none>"


def _marker_diffs(reference: Document, other: Document, translation: str) -> list[str]:
    """A heading reordered while the level sequence still matches."""
    reference_seq = tuple(m for m in reference.heading_markers if m)
    other_seq = tuple(m for m in other.heading_markers if m)
    if reference_seq == other_seq:
        return []
    limit = min(len(reference_seq), len(other_seq))
    index = next(
        (position for position in range(limit) if reference_seq[position] != other_seq[position]),
        limit,
    )
    reference_at = _format_marker(reference_seq[index]) if index < len(reference_seq) else "<end>"
    other_at = _format_marker(other_seq[index]) if index < len(other_seq) else "<end>"
    return [
        f"heading markers diverge at position {index} ({translation}):"
        f" reference {reference_at} vs translation {other_at}"
    ]


def _pair_sections(reference: Document, other: Document) -> tuple[tuple[tuple[int, int], ...], int]:
    """Pair sections by marker fingerprint, then by occurrence index.

    Returns the pairs and the number of index-fallback pairs. Fingerprints are
    matched in occurrence order, so duplicated markers stay aligned.
    """
    pairs: list[tuple[int, int]] = []
    matched_reference: set[int] = set()
    matched_other: set[int] = set()
    buckets: dict[tuple[str, ...], list[int]] = {}
    for index, fingerprint in enumerate(other.heading_markers):
        if fingerprint:
            buckets.setdefault(fingerprint, []).append(index)
    for index, fingerprint in enumerate(reference.heading_markers):
        if not fingerprint:
            continue
        bucket = buckets.get(fingerprint)
        if bucket:
            other_index = bucket.pop(0)
            pairs.append((index, other_index))
            matched_reference.add(index)
            matched_other.add(other_index)
    remaining_reference = [
        index for index in range(len(reference.heading_markers)) if index not in matched_reference
    ]
    remaining_other = [
        index for index in range(len(other.heading_markers)) if index not in matched_other
    ]
    fallback = 0
    for index, other_index in zip(remaining_reference, remaining_other):
        pairs.append((index, other_index))
        fallback += 1
    return tuple(pairs), fallback


def _invariant_token_gaps(reference: Document, other: Document) -> list[str]:
    """Paired sections whose translation lost code-span/path invariants."""
    if len(reference.section_texts) != len(other.section_texts):
        return []
    parts: list[str] = []
    pairs, _ = _pair_sections(reference, other)
    for reference_index, other_index in pairs:
        reference_tokens = _invariant_tokens(reference.section_texts[reference_index])
        strong_reference = {token for token in reference_tokens if _is_strong_token(token)}
        if not strong_reference:
            continue
        missing = _missing_tokens(reference_tokens, other.section_texts[other_index])
        strong_missing = {token for token in missing if _is_strong_token(token)}
        complete_omission = strong_missing == strong_reference
        if len(strong_missing) < MIN_MISSING_INVARIANT_TOKENS and not complete_omission:
            continue
        listed = sorted(strong_missing) + sorted(
            token for token in missing if token not in strong_missing
        )
        parts.append(
            f"section #{reference_index} ({reference.headings[reference_index]}) missing"
            f" invariant tokens: {', '.join(_token_label(token) for token in listed)}"
        )
    return parts


def _structural_diffs(reference: Document, other: Document, translation: str) -> list[str]:
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
    parts.extend(_marker_diffs(reference, other, translation))
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


_THEMATIC_BREAK = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


def _meaningful_body_chars(text: str) -> int:
    """Body length ignoring blank lines and Markdown thematic breaks.

    A ``---`` separator carries no prose and is not a translation obligation, so
    it must not make a section look non-empty (the client READMEs end their
    directory-structure section with one).
    """
    return sum(
        len(line.strip())
        for line in text.splitlines()
        if line.strip() and not _THEMATIC_BREAK.match(line)
    )


def _tiny_empty_sections(reference: Document, other: Document) -> list[str]:
    """English sections below the ratio band whose translation body is empty.

    The ratio gates skip an English section shorter than
    ``EMPTY_SECTION_REFERENCE_CHARS`` entirely; this closes that hole by
    asserting the translation is non-empty whenever English carries real
    (non-separator) content. Only sections the ratio gates do not already cover
    are reported, so a section is never flagged twice.
    """
    if len(reference.section_texts) != len(other.section_texts):
        return []
    parts: list[str] = []
    for index, (ref_chars, ref_text) in enumerate(zip(reference.bodies, reference.section_texts)):
        if ref_chars >= EMPTY_SECTION_REFERENCE_CHARS:
            continue
        if _meaningful_body_chars(ref_text) == 0:
            continue
        if _meaningful_body_chars(other.section_texts[index]) == 0:
            parts.append(
                f"section #{index} ({reference.headings[index]}) translation body is empty"
                f" (EN body {ref_chars} chars, below the {EMPTY_SECTION_REFERENCE_CHARS}-char"
                " ratio band)"
            )
    return parts


def _format_languages(suffixes: set[str]) -> str:
    return ", ".join(sorted(suffixes)) if suffixes else "the unlocalized base"


def _link_language_diffs(reference: Document, other: Document, translation: str) -> list[str]:
    """Links pointing at a different language than the English reference.

    The base-language suffix and the translation's own suffix are both
    self-references and are removed from both sides (see the normalization note
    at ``_LOCALIZED_README``); a remaining mismatch means the translation links
    a foreign README the reference does not. Targets present on only one side
    are left to the missing/extra link check, so nothing is reported twice.
    """
    own = _OWN_SUFFIX[translation]
    reference_map = dict(reference.link_suffixes)
    other_map = dict(other.link_suffixes)
    parts: list[str] = []
    for target in sorted(set(reference_map) & set(other_map)):
        reference_foreign = set(reference_map[target]) - {"", own}
        other_foreign = set(other_map[target]) - {"", own}
        if reference_foreign != other_foreign:
            parts.append(
                f"link target {target} points at a different language:"
                f" reference {_format_languages(reference_foreign)} vs"
                f" {translation} {_format_languages(other_foreign)}"
            )
    return parts


def _semantic_diffs(reference: Document, other: Document, translation: str) -> list[str]:
    """Every second-order mismatch of ``other`` against the reference."""
    parts: list[str] = []
    missing = sorted(reference.links - other.links)
    extra = sorted(other.links - reference.links)
    if missing:
        parts.append(f"links missing: {missing}")
    if extra:
        parts.append(f"links extra: {extra}")
    parts.extend(_link_language_diffs(reference, other, translation))
    for gap in _section_gaps(reference, other):
        parts.append(
            f"section #{gap.index} ({gap.heading}) body ratio {gap.ratio:.3f}"
            f" < {NEAR_EMPTY_RATIO:.2f} (EN {gap.reference_chars} chars,"
            f" translated {gap.translation_chars} chars)"
        )
    parts.extend(_tiny_empty_sections(reference, other))
    parts.extend(_invariant_token_gaps(reference, other))
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
            continue
        if ALLOWLIST_PLACEHOLDER.match(text):
            violations.append(f"{group}: reason starts with placeholder text: {text!r}")
            continue
        clause = _ALLOWLIST_FIX_CLAUSE.search(text)
        if clause is None:
            violations.append(
                f"{group}: reason must argue why fixing the document(s) is wrong"
                " (e.g. 'Fixing the document(s) is wrong because ...')"
            )
            continue
        rationale = clause.group("why").strip()
        if len(rationale) < MIN_ALLOWLIST_FIX_RATIONALE_CHARS:
            violations.append(
                f"{group}: the 'why fixing is wrong' clause has only {len(rationale)} chars,"
                f" below the {MIN_ALLOWLIST_FIX_RATIONALE_CHARS}-char rationale minimum"
            )
    discovered = {str(group_path.relative_to(root)) for group_path in groups}
    for group in sorted(set(ALLOWLIST) - discovered):
        violations.append(f"{group}: allowlisted group was not discovered (stale exemption)")
    return violations


def _out(text: str) -> None:
    """Write one line to stdout (keeps ruff's flake8-print gate happy)."""
    sys.stdout.write(text + "\n")


def _format_signature(document: Document) -> str:
    markers = sum(1 for fingerprint in document.heading_markers if fingerprint)
    return (
        f"headings={len(document.heading_levels)} "
        f"markers={markers} "
        f"fences={document.fences} "
        f"tables={document.table_rows} "
        f"links={len(document.links)}"
    )


def _percentile(values: list[float], quantile: float) -> float:
    """Linear-interpolated percentile (the numpy default method)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = quantile * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def calibration_stats(root: Path = REPO_ROOT) -> tuple[LanguageCalibration, ...]:
    """Per-language ratio distribution over sections with a large EN body."""
    ratios: dict[str, list[float]] = {translation: [] for translation in TRANSLATIONS}
    for group in discover_groups(root):
        reference = extract(group / REFERENCE)
        for translation in TRANSLATIONS:
            other = extract(group / translation)
            if len(reference.bodies) != len(other.bodies):
                continue
            for ref_chars, tr_chars in zip(reference.bodies, other.bodies):
                if ref_chars >= MIN_REFERENCE_BODY_CHARS:
                    ratios[translation].append(tr_chars / ref_chars)
    return tuple(
        LanguageCalibration(
            translation,
            len(values),
            min(values, default=0.0),
            _percentile(values, 0.01),
            _percentile(values, 0.05),
            _percentile(values, 0.50),
        )
        for translation, values in ratios.items()
    )


def print_calibration(root: Path = REPO_ROOT) -> None:
    """Print the ratio distribution and its margin above the band; never gates."""
    stats = calibration_stats(root)
    _out(
        f"README parity calibration — band {NEAR_EMPTY_RATIO:.2f},"
        f" sections with EN body >= {MIN_REFERENCE_BODY_CHARS} chars"
    )
    for cal in stats:
        _out(
            f"  {cal.language}: pairs={cal.pairs} min={cal.minimum:.3f}"
            f" p1={cal.p1:.3f} p5={cal.p5:.3f} median={cal.median:.3f}"
            f" margin={cal.margin:.3f}x"
        )
    smallest = min((cal.minimum for cal in stats), default=0.0)
    _out(
        f"  smallest legitimate ratio {smallest:.3f} keeps"
        f" {smallest / NEAR_EMPTY_RATIO:.3f}x the band;"
        f" required minimum margin {MIN_CALIBRATION_MARGIN:.2f}x"
    )


def main(root: Path = REPO_ROOT, *, calibrate: bool = False) -> int:
    """Run the parity gate over ``root``; return the process exit code."""
    if calibrate:
        print_calibration(root)
        return 0
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
    fingerprinted = 0
    fallback = 0
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
            pairs, fallback_pairs = _pair_sections(reference, other)
            fingerprinted += len(pairs) - fallback_pairs
            fallback += fallback_pairs
            parts = _structural_diffs(reference, other, translation) + _semantic_diffs(
                reference, other, translation
            )
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
    _out(f"section pairing: {fingerprinted} by heading marker, {fallback} by occurrence index")
    _out(
        "pairing note: reordering among headings without inline code / link markers"
        " is not detectable (residual limitation)"
    )
    _out(f"allowlisted: {allowlisted}")
    for group in sorted(ALLOWLIST):
        _out(f"  - {group}: {ALLOWLIST[group]}")
    _out("VERDICT: PASS" if failures == 0 else "VERDICT: FAIL")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(calibrate="--calibrate" in sys.argv[1:]))
