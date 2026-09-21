#!/usr/bin/env python3
"""Markdown dead-link and bad-anchor gate.

Every Markdown file is scanned for local links (and images) whose target does
not exist, and for ``#fragment`` anchors that do not match any heading in the
target document. External URLs, ``{{...}}`` template placeholders, and content
inside fenced code blocks are not links and are skipped.

Resolution semantics (why a naive checker reports false positives):

  * relative links resolve against the *directory of the containing file*;
  * links starting with ``/`` inside ``client/docs/**`` are VitePress
    root-absolute routes, not filesystem paths: ``/types/reference`` maps to
    ``client/docs/types/reference.md`` (extensionless routes also try
    ``<route>.md`` and ``<route>/index.md``);
  * a ``#fragment`` is percent-decoded before comparison and matched against
    the GitHub heading slug (``github-slugger`` semantics: lowercase, drop
    punctuation/symbols, keep letters/marks/digits plus ``-``/``_``, spaces to
    hyphens, duplicate headings suffixed ``-1``/``-2``). Variation selectors
    (U+FE0E/U+FE0F) are treated as absent on both sides: they are invisible
    presentation characters, and this repository carries both the
    ``%EF%B8%8F-...`` and ``-...`` encodings of the same emoji anchor.

Exemptions (``ALLOWLIST``) follow the contract used by ``check_docs_parity.py``:
a reason must be substantive, not open with placeholder text, and must contain
an explicit "why fixing the documents instead is wrong" clause with a real
rationale. A stale entry (no longer triggered) fails the gate.

Usage: ``uv run --no-sync python scripts/check_doc_links.py``
Exit codes: 0 = every local link resolves; 1 = a finding or a contract breach.
"""

# allow: SIZE_OK - single-purpose CI gate: the module docstring documents the
# resolution and exemption contracts, and the scanner is one linear
# parse -> resolve -> report pipeline; splitting it would only add import
# indirection to a standalone script. Mirrors the SIZE_OK exception on
# scripts/check_docs_parity.py.

from __future__ import annotations

import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories that never hold first-party documentation worth gating.
SKIP_DIRS = frozenset(
    {
        ".codegraph",
        ".git",
        ".grimp_cache",
        ".import_linter_cache",
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
# Generated/ignored trees that are absent on a clean checkout; skipped so a
# local run and a CI run scan exactly the same file set.
SKIP_PATHS = ("evals/results",)

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_LINK = re.compile(r"(?<!!)\[([^\]]*)\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_EXTERNAL = ("http://", "https://", "mailto:", "tel:", "//")
_VARIATION_SELECTORS = "\ufe0e\ufe0f"

# Allowlist contract (same discipline as scripts/check_docs_parity.py).
MIN_ALLOWLIST_REASON_CHARS = 40
MIN_ALLOWLIST_FIX_RATIONALE_CHARS = 20
ALLOWLIST_PLACEHOLDER = re.compile(r"^\s*(?:skip|n/?a|todo|tbd|fixme|wip|placeholder)\b", re.I)
_ALLOWLIST_FIX_CLAUSE = re.compile(
    r"(?:Fixing the documents? is wrong because"
    r"|Cannot be fixed in the documents? because"
    r"|The documents? cannot be fixed because)"
    r"\s+(?P<why>.+)$",
    re.IGNORECASE | re.DOTALL,
)

_LICENSE_DEFERRED_REASON = (
    "Fixing the documents is wrong because these links deliberately point at the"
    " project's top-level LICENSE, which the repository declares as MIT but does"
    " not yet ship; the copyright holder cannot be uniquely derived"
    ' (client/src-tauri/Cargo.toml authors = "EMA AI Agent Team" versus the sole'
    ' git author and repository owner "lasertsole <3132225629@qq.com>"), so'
    " creating the file is an owner decision, not a documentation rewrite."
)

# (source path relative to the repo root, raw link destination) -> reason.
ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "skills/builtin/core/multimodal_rag/README.md",
        "../../../../LICENSE",
    ): (_LICENSE_DEFERRED_REASON),
    (
        "skills/builtin/core/multimodal_rag/README.zh.md",
        "../../../../LICENSE",
    ): (_LICENSE_DEFERRED_REASON),
    (
        "skills/builtin/core/multimodal_rag/README.ja.md",
        "../../../../LICENSE",
    ): (_LICENSE_DEFERRED_REASON),
    (
        "skills/builtin/core/multimodal_rag/README.ko.md",
        "../../../../LICENSE",
    ): (_LICENSE_DEFERRED_REASON),
    (
        "skills/builtin/code_wiki/templates/getting-started.md",
        "diagrams/",
    ): (
        "Fixing the documents is wrong because skills/builtin/code_wiki/templates/ is"
        " a scaffold instantiated into a generated wiki elsewhere; diagrams/ is"
        " materialised by the code_wiki generator in the generated tree, so it"
        " intentionally does not exist beside the template."
    ),
}


@dataclass(frozen=True)
class Finding:
    """One unresolved local link or anchor."""

    source: Path
    line: int
    dest: str
    reason: str


def _out(text: str) -> None:
    """Write one line to stdout (keeps ruff's flake8-print gate happy)."""
    sys.stdout.write(text + "\n")


def _slug(text: str) -> str:
    """Approximate GitHub's ``github-slugger`` for one heading's plain text.

    Keeps letters/marks/digits plus ``-``/``_`` and ASCII space, drops every
    other code point (punctuation, symbols, emoji bases), then maps spaces to
    hyphens. Variation selectors are dropped on the way in so the two emoji
    encodings in this repository compare equal.
    """
    kept: list[str] = []
    for char in text.translate({ord(c): None for c in _VARIATION_SELECTORS}).lower():
        if char == " " or char in "-_" or unicodedata.category(char)[0] in ("L", "M", "N"):
            kept.append(char)
    return "".join(kept).replace(" ", "-")


def _strip_title(dest: str) -> str:
    """Drop an optional ``<...>`` wrapper and surrounding whitespace."""
    dest = dest.strip()
    return dest[1:-1] if len(dest) >= 2 and dest[0] == "<" and dest[-1] == ">" else dest


def _is_external(dest: str) -> bool:
    return dest.startswith(_EXTERNAL) or "://" in dest


def _headings(text: str) -> list[str]:
    """Headings outside fenced code blocks, backticks removed, prose kept."""
    found: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    for raw in text.splitlines():
        fence = _FENCE.match(raw)
        if fence is not None:
            run = fence.group(1)
            if not in_fence:
                in_fence, fence_char, fence_len = True, run[0], len(run)
            elif run[0] == fence_char and len(run) >= fence_len:
                in_fence = False
            continue
        if not in_fence:
            heading = _HEADING.match(raw)
            if heading is not None:
                found.append(heading.group(2).replace("`", ""))
    return found


def _slug_set(text: str) -> frozenset[str]:
    """Every anchor GitHub would generate for the document (duplicates suffixed)."""
    seen: dict[str, int] = {}
    slugs: set[str] = set()
    for heading in _headings(text):
        base = _slug(heading)
        count = seen.get(base, 0)
        slugs.add(base if count == 0 else f"{base}-{count}")
        seen[base] = count + 1
    return frozenset(slugs)


def _iter_links(text: str) -> list[tuple[int, str]]:
    """Inline link and image destinations outside fences, with line numbers."""
    found: list[tuple[int, str]] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    for line_number, raw in enumerate(text.splitlines(), 1):
        fence = _FENCE.match(raw)
        if fence is not None:
            run = fence.group(1)
            if not in_fence:
                in_fence, fence_char, fence_len = True, run[0], len(run)
            elif run[0] == fence_char and len(run) >= fence_len:
                in_fence = False
            continue
        if in_fence:
            continue
        line = _INLINE_CODE.sub("", raw)
        for match in (*_LINK.finditer(line), *_IMAGE.finditer(line)):
            found.append((line_number, _strip_title(match.group(2))))
    return found


def _walk(root: Path) -> list[Path]:
    """Every Markdown file under ``root`` outside the skip set, sorted."""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        relative = Path(dirpath).relative_to(root).as_posix()
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        if relative != "." and relative.startswith(SKIP_PATHS):
            dirnames[:] = []
            continue
        files.extend(Path(dirpath) / name for name in filenames if name.endswith(".md"))
    return sorted(files)


def _resolve(path: str, source: Path, root: Path) -> Path | None:
    """Resolve a link path to an existing file or directory, or ``None``."""
    if not path:
        return source
    if not path.startswith("/"):
        candidate = (source.parent / path).resolve()
        return candidate if candidate.exists() else None
    # VitePress root-absolute route, only meaningful inside client/docs.
    try:
        source.relative_to(root / "client" / "docs")
    except ValueError:
        return None
    base = (root / "client" / "docs" / path.lstrip("/")).resolve()
    candidates = (base.with_suffix(".md"), base, base / "index.md") if not base.suffix else (base,)
    return next((candidate for candidate in candidates if candidate.exists()), None)


@dataclass(frozen=True)
class _Scan:
    """Cached document text and its anchor set."""

    text: str
    slugs: frozenset[str]


def _check(root: Path, files: list[Path]) -> tuple[list[Finding], int]:
    """Every unresolved local link/anchor across ``files``, and the checked count."""
    cache: dict[Path, _Scan] = {}

    def scan(path: Path) -> _Scan | None:
        if path not in cache:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return None
            cache[path] = _Scan(text, _slug_set(text))
        return cache[path]

    findings: list[Finding] = []
    checked = 0
    for source in files:
        document = scan(source)
        if document is None:
            continue
        for line, raw_dest in _iter_links(document.text):
            if not raw_dest or _is_external(raw_dest) or "{{" in raw_dest or "}}" in raw_dest:
                continue
            checked += 1
            path, _, fragment = raw_dest.partition("#")
            target = _resolve(unquote(path), source, root)
            if target is None:
                findings.append(Finding(source, line, raw_dest, "dead path"))
                continue
            if not fragment or target.suffix != ".md":
                continue
            target_document = scan(target)
            if target_document is None:
                continue
            decoded = unquote(fragment).translate({ord(c): None for c in _VARIATION_SELECTORS})
            if decoded not in target_document.slugs:
                findings.append(Finding(source, line, raw_dest, "bad anchor"))
    return findings, checked


def _allowlist_key(source: Path, dest: str, root: Path) -> tuple[str, str]:
    return (source.relative_to(root).as_posix(), dest)


def _violations(findings: list[Finding], root: Path) -> list[str]:
    """Allowlist contract breaches and stale entries."""
    violations: list[str] = []
    triggered: set[tuple[str, str]] = set()
    for finding in findings:
        key = _allowlist_key(finding.source, finding.dest, root)
        if key in ALLOWLIST:
            triggered.add(key)
    for key, reason in sorted(ALLOWLIST.items()):
        text = reason.strip()
        if len(text) < MIN_ALLOWLIST_REASON_CHARS:
            violations.append(
                f"{key}: reason is {len(text)} chars,"
                f" below the {MIN_ALLOWLIST_REASON_CHARS}-char minimum"
            )
            continue
        if ALLOWLIST_PLACEHOLDER.match(text):
            violations.append(f"{key}: reason starts with placeholder text: {text!r}")
            continue
        clause = _ALLOWLIST_FIX_CLAUSE.search(text)
        if clause is None:
            violations.append(
                f"{key}: reason must argue why fixing the document(s) is wrong"
                " (e.g. 'Fixing the documents is wrong because ...')"
            )
            continue
        if len(clause.group("why").strip()) < MIN_ALLOWLIST_FIX_RATIONALE_CHARS:
            violations.append(f"{key}: the 'why fixing is wrong' clause has too little rationale")
    for key in sorted(set(ALLOWLIST) - triggered):
        violations.append(f"{key}: allowlisted link no longer triggers (stale exemption)")
    return violations


def main(root: Path = REPO_ROOT) -> int:
    """Run the gate over ``root``; return the process exit code."""
    files = _walk(root)
    findings, checked = _check(root, files)
    violations = _violations(findings, root)
    allowlisted = [
        finding
        for finding in findings
        if _allowlist_key(finding.source, finding.dest, root) in ALLOWLIST
    ]
    reported = [finding for finding in findings if finding not in allowlisted]

    _out(
        f"Markdown link gate — {len(files)} file(s) scanned,"
        f" {checked} local link(s) checked under {root}"
    )
    for violation in violations:
        _out(f"ALLOWLIST {violation}")
    dead = sum(1 for finding in reported if finding.reason == "dead path")
    anchors = len(reported) - dead
    for finding in reported:
        _out(
            f"{finding.reason.upper()} {finding.source.relative_to(root)}:"
            f"{finding.line} -> {finding.dest}"
        )
    _out(f"{len(reported)} finding(s): {dead} dead path(s), {anchors} bad anchor(s)")
    _out(f"allowlisted: {len(allowlisted)}")
    for key in sorted(ALLOWLIST):
        _out(f"  - {key[0]} -> {key[1]}")
    failed = bool(reported) or bool(violations)
    _out("VERDICT: FAIL" if failed else "VERDICT: PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
