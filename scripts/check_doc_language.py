#!/usr/bin/env python3
"""Wrong-language-labels gate for the four-language docs.

The docs are translations, but their prose is authored from the Chinese
specification, so a label from ANOTHER language can slip into a page and still
pass every other gate: the parity gate compares STRUCTURE across languages, and
the link gate only resolves targets. Live-verified twice — Chinese UI labels in
the English README's Runtime section and six English wiki pages, and 「模型配置」
inside the Japanese and Korean READMEs.

Rules per document kind:

  * **English** (``README.md`` + every ``wiki/**/README.md``, nested pages
    included): no CJK ideographs at all.
  * **Japanese / Korean** (``README.ja.md`` / ``README.ko.md`` and their wiki
    siblings): no SIMPLIFIED-CHINESE-ONLY characters (the shared list in
    ``scripts/data/simplified_chinese_only.txt``). Kanji Japanese also uses are
    fine; a Simplified-only form (时, 图, 问 …) is a Chinese leak. A label built
    only from shared characters (模型配置) is not detectable — this gate targets
    mixed-language text, not every possible label.
  * **Chinese** pages are the source language and are NOT scanned: a Chinese
    page legitimately contains English names, paths and product terms.

Ignored within a scanned document (so a legitimate sentence passes):

  * fenced code blocks and inline code spans — paths, commands and identifiers
    are code, and a Chinese path inside one is not prose;
  * markdown link TARGETS (``](…)``) — a link may POINT at a Chinese-named file
    (a TODO plan, the localized page); only the visible label must be in the
    document's language. The Chinese README's own labels live in files this gate
    never opens;
  * ``ALLOWED_TOKENS`` — the four language NAMES, which a language switcher
    line must spell in their own script.

Escape hatch: when a document genuinely needs the other language's label, put
it in a code span (or a link target) — the gate stays silent there. Do not grow
``ALLOWED_TOKENS`` for that.

Usage: ``uv run --no-sync python scripts/check_doc_language.py [--root DIR]``
Exit codes: 0 = clean; 1 = at least one finding.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]

#: The Simplified-only character list (shared with the client locale tests so
#: the two gates cannot drift).
SIMPLIFIED_ONLY_PATH = ROOT / "scripts" / "data" / "simplified_chinese_only.txt"

#: Document language → the pages it applies to, in ONE table.
DocLang = Literal["en", "ja", "ko"]

DOCS: tuple[tuple[DocLang, str], ...] = (
    ("en", "README.md"),
    ("en", "wiki/**/README.md"),
    ("ja", "README.ja.md"),
    ("ja", "wiki/**/README.ja.md"),
    ("ko", "README.ko.md"),
    ("ko", "wiki/**/README.ko.md"),
)

#: CJK ideographs (the English rule bans all of them).
CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")

#: Tokens that may appear in ANY document: the interface language names (the
#: language switcher spells each in its own script).
ALLOWED_TOKENS: tuple[str, ...] = ("简体中文", "中文", "日本語", "한국어")

#: Fence openers (``` and ~~~, any info string).
_FENCE = re.compile(r"^\s*(```|~~~)")
#: A markdown link/image target: the ``](…)`` part. Labels stay prose.
_LINK_TARGET = re.compile(r"\]\([^)]*\)")
#: An HTML comment, possibly spanning lines.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
#: An inline code span (one or more backticks, same count closing).
_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)*?\1")


@dataclass(frozen=True)
class Finding:
    """One wrong-language run found in a document."""

    path: Path
    line: int
    run: str
    detail: str
    context: str

    def render(self) -> str:
        """One-line, greppable report."""
        return f"{self.path}:{self.line}: {self.run} ({self.detail}) — {self.context}"


def load_simplified_only(path: Path = SIMPLIFIED_ONLY_PATH) -> frozenset[str]:
    """Read the Simplified-only character set (comment lines start with ``#``)."""
    text = path.read_text(encoding="utf-8")
    chars: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        chars.update(char for char in stripped if not char.isspace())
    if not chars:
        raise RuntimeError(f"no Simplified-only characters found in {path}")
    return frozenset(chars)


def _blank(text: str) -> str:
    """Replace every non-newline character with a space (keeps line numbers)."""
    return "".join("\n" if char == "\n" else " " for char in text)


def prose_lines(text: str) -> list[tuple[int, str]]:
    """The document's prose, line by line, with code/targets/comments removed.

    Line numbers are preserved: every stripped region is blanked in place, so a
    finding points at the real line in the real file.
    """
    # HTML comments first (they may span lines and may contain fences).
    text = _HTML_COMMENT.sub(lambda match: _blank(match.group(0)), text)

    out: list[tuple[int, str]] = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append((number, ""))
            continue
        if in_fence:
            out.append((number, ""))
            continue
        # Link targets are addresses, not prose; inline code is code.
        stripped = _LINK_TARGET.sub("]()", line)
        stripped = _CODE_SPAN.sub(" ", stripped)
        out.append((number, stripped))
    return out


def _without_allowed_tokens(line: str) -> str:
    for token in ALLOWED_TOKENS:
        line = line.replace(token, " ")
    return line


def _finding(path: Path, number: int, match: re.Match[str], line: str, detail: str) -> Finding:
    """Build a finding with a bounded context window around the match."""
    start = max(0, match.start() - 30)
    end = min(len(line), match.end() + 30)
    return Finding(
        path=path,
        line=number,
        run=match.group(0),
        detail=detail,
        context=line[start:end].strip(),
    )


def scan_text(
    path: Path,
    text: str,
    lang: DocLang,
    simplified_only: frozenset[str] | None = None,
) -> list[Finding]:
    """Find wrong-language prose in one document."""
    findings: list[Finding] = []
    only = simplified_only
    if lang != "en" and only is None:
        only = load_simplified_only()
    for number, line in prose_lines(text):
        scrubbed = _without_allowed_tokens(line)
        for match in CJK_RUN.finditer(scrubbed):
            if lang == "en":
                findings.append(_finding(path, number, match, scrubbed, "CJK in English prose"))
                continue
            offenders = sorted({char for char in match.group(0) if only and char in only})
            if offenders:
                findings.append(
                    _finding(
                        path,
                        number,
                        match,
                        scrubbed,
                        f"Simplified Chinese {''.join(offenders)} in a {lang} document",
                    )
                )
    return findings


def documents(root: Path) -> list[tuple[DocLang, Path]]:
    """The scanned documents under *root* as ``(language, path)`` pairs."""
    found: dict[Path, DocLang] = {}
    for lang, pattern in DOCS:
        for path in root.glob(pattern):
            if path.is_file():
                found[path] = lang
    return [(found[path], path) for path in sorted(found)]


def main(argv: list[str] | None = None) -> int:
    """Scan every localized page and print a verdict."""
    parser = argparse.ArgumentParser(
        description=(__doc__ or "Wrong-language-labels gate").splitlines()[0]
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Repository root to scan (defaults to this checkout).",
    )
    args = parser.parse_args(argv)
    root: Path = args.root

    simplified_only = load_simplified_only(SIMPLIFIED_ONLY_PATH)
    files = documents(root)
    findings: list[Finding] = []
    for lang, path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"  ! unreadable: {path}: {exc}")
            findings.append(Finding(path, 0, "<unreadable>", "unreadable", str(exc)))
            continue
        findings.extend(scan_text(path, text, lang, simplified_only))

    by_lang: dict[str, int] = {}
    for lang, _path in files:
        by_lang[lang] = by_lang.get(lang, 0) + 1
    summary = ", ".join(f"{count} {lang}" for lang, count in sorted(by_lang.items()))
    print(f"Wrong-language-labels gate — {len(files)} file(s) scanned under {root} ({summary})")
    for finding in findings:
        print(f"  {finding.render()}")
    print(f"{len(findings)} finding(s)")
    if findings:
        print("VERDICT: FAIL")
        print(
            "  Translate the label into the document's language (see the matching "
            "locale JSON), or move it into a code span if it is an identifier.",
            file=sys.stderr,
        )
        return 1
    print("VERDICT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
