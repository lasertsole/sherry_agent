#!/usr/bin/env python3
"""No-CJK-in-English-docs gate.

The four root READMEs (and the wiki pages) are translations, but their prose is
authored from the Chinese specification, so a Chinese UI label (模型配置,
继续生成 …) can slip into an English sentence and still pass every other gate:
the parity gate compares STRUCTURE across languages, and the link gate only
resolves targets. Live-verified in the English README's Runtime section and in
five English wiki pages.

Scanned (the ENGLISH documents only):

  * ``README.md``;
  * every ``wiki/**/README.md`` (the English page of each four-language group).

Deliberately NOT scanned, because they are supposed to be non-English:

  * ``README.zh.md`` / ``.ja.md`` / ``.ko.md`` and the localized wiki pages
    (``README.zh.md`` …) — Chinese/Japanese/Korean text is their whole point;
  * ``AGENTS.md`` — the developer guide quotes the Chinese UI labels on purpose
    when it names them (继续生成, 分叉, 会话分支 …). It is not a product README.

Ignored within a scanned document (so a legitimate English sentence passes):

  * fenced code blocks and inline code spans — paths, commands and identifiers
    are code, and a Chinese path inside one is not prose;
  * markdown link TARGETS (``](…)``) — a link may legitimately POINT at a
    Chinese-named file (a TODO plan, a localized page); only the visible label
    must be English. The Chinese README's own link labels live in files this
    gate never opens;
  * ``ALLOWED_TOKENS`` (below): the four LANGUAGE NAMES, which the language
    switcher line and any sentence about interface languages must spell in
    their own script.

Escape hatch: when an English document genuinely needs a Chinese label, put it
in a code span (or a link target) — the gate stays silent there. Do not grow
``ALLOWED_TOKENS`` for that.

Usage: ``uv run --no-sync python scripts/check_doc_language.py [--root DIR]``
Exit codes: 0 = clean; 1 = at least one CJK run in English prose.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: English documents scanned. The localized siblings (``README.zh.md`` …) never
#: match these names.
DOC_PATTERNS: tuple[str, ...] = ("README.md", "wiki/**/README.md")

#: CJK ideographs. Kana/hangul are NOT scanned: a Japanese or Korean term can
#: legitimately appear as a quoted proper name in English prose, and the leak
#: this gate exists for is Chinese labels carried over from the spec.
CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")

#: Tokens that may appear inside English prose: the interface language names
#: (the language switcher spells each in its own script) and their common forms.
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
    """One CJK run found in English prose."""

    path: Path
    line: int
    run: str
    context: str

    def render(self) -> str:
        """One-line, greppable report."""
        return f"{self.path}:{self.line}: {self.run} — {self.context}"


def _blank(text: str) -> str:
    """Replace every non-newline character with a space (keeps line numbers)."""
    return "".join("\n" if char == "\n" else " " for char in text)


def prose_lines(text: str) -> list[tuple[int, str]]:
    """The document's prose, line by line, with code/link targets/comments removed.

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


def scan_text(path: Path, text: str) -> list[Finding]:
    """Find CJK runs in one document's English prose."""
    findings: list[Finding] = []
    for number, line in prose_lines(text):
        scrubbed = _without_allowed_tokens(line)
        for match in CJK_RUN.finditer(scrubbed):
            start = max(0, match.start() - 30)
            end = min(len(scrubbed), match.end() + 30)
            findings.append(
                Finding(
                    path=path,
                    line=number,
                    run=match.group(0),
                    context=scrubbed[start:end].strip(),
                )
            )
    return findings


def documents(root: Path) -> list[Path]:
    """The English documents under *root* (deduplicated, sorted)."""
    found: dict[Path, None] = {}
    for pattern in DOC_PATTERNS:
        for path in root.glob(pattern):
            if path.is_file() and not path.name.startswith(("README.zh", "README.ja", "README.ko")):
                found[path] = None
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    """Scan every English document and print a verdict."""
    parser = argparse.ArgumentParser(
        description=(__doc__ or "No-CJK-in-English-docs gate").splitlines()[0]
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Repository root to scan (defaults to this checkout).",
    )
    args = parser.parse_args(argv)
    root: Path = args.root

    files = documents(root)
    findings: list[Finding] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"  ! unreadable: {path}: {exc}")
            findings.append(Finding(path, 0, "<unreadable>", str(exc)))
            continue
        findings.extend(scan_text(path, text))

    print(f"No-CJK-in-English-docs gate — {len(files)} file(s) scanned under {root}")
    for finding in findings:
        print(f"  {finding.render()}")
    print(f"{len(findings)} finding(s)")
    if findings:
        print("VERDICT: FAIL")
        print(
            "  Translate the label to the UI language being documented "
            "(en.json), or move it into a code span if it is an identifier.",
            file=sys.stderr,
        )
        return 1
    print("VERDICT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
