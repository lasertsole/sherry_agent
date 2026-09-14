#!/usr/bin/env python3
"""Backstop security scan for the vendored RAG trees (audit item #58).

Why this exists
---------------
``pyproject.toml`` fully excludes ``vendored_lightrag/`` and ``vendored_raganything/``
from basedpyright, and the pinned ruff selection carries only ``S110`` from
flake8-bandit. Those trees broker API keys for several LLM providers, so a
hardcoded secret or a dangerous call there would pass every existing gate.

Why regex rules instead of ruff's ``S`` rules
---------------------------------------------
Measured with ruff 0.16.7 before writing this file: ``--select S`` over the two
vendored trees reports 28 findings, nearly all upstream idiom noise (``S324``
hashlib, ``S603``/``S607`` pandoc/mineru subprocesses, ...). More importantly
the bandit rules do not see the audited risk: ``API_KEY = "sk-..."`` is not
flagged by ``S105`` (it matches password/secret/token-style names, not
``api_key``), and there is no rule for known credential prefixes at all. A
path-scoped scanner with an explicit allowlist keeps the signal high exactly
where the audit found the hole.

Usage
-----
    uv run --no-sync python scripts/checks/check_vendored_security.py [paths...]

Without arguments the two vendored roots are scanned. With paths (the
pre-commit hook passes them) exactly those are scanned; missing paths are
skipped so file deletions never fail the hook. Exit codes: 0 = clean,
1 = un-baselined hit, 2 = malformed baseline file.

Baseline
--------
``vendored_security_baseline.txt`` holds reviewed, per-hit exemptions. A hit
is exempted only when rule id, repo-relative path and snippet fingerprint all
match; there are no globs or path prefixes. Fingerprints are printed next to
every un-baselined hit and never store the matched secret in the file.
"""

# allow: SIZE_OK - single-purpose scanner; ~40% is the pure-data rule table,
# and splitting it from the classification logic that gives it meaning would
# hide the rules' false-positive rationale.

from __future__ import annotations

import hashlib
import os
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDORED_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "skills/builtin/core/multimodal_rag/scripts/graph_rag/vendored_lightrag",
    REPO_ROOT / "skills/builtin/core/multimodal_rag/scripts/graph_rag/vendored_raganything",
)
SOURCE_SUFFIXES = frozenset({".py", ".pyi"})
BASELINE_PATH = Path(__file__).with_name("vendored_security_baseline.txt")

# Words that mark a name as secret-bearing. api_key/apikey are the reason this
# scanner exists: bandit's S105 does not include them.
_SECRET_WORD = (
    r"(?:api[_-]?key|apikey|secret|token|passwd|password|credential|"
    r"auth[_-]?key|access[_-]?key|private[_-]?key)"
)
# Broad candidate: any identifier containing a secret word, then validated
# segment-by-segment so `tokenizer_model`/`max_tokens` never match.
_SECRET_NAME = rf"[A-Za-z0-9_]*{_SECRET_WORD}[A-Za-z0-9_]*"
_SECRET_LITERAL = r"""(?P<quote>["'])(?P<secret>[^"'\n]{8,})(?P=quote)"""

# Singular credential words are secret-bearing; `tokens` is a count, so plural
# is deliberately absent. Pairs cover api_key / auth_key / access_key / private_key.
_SECRET_SEGMENTS = frozenset(
    {"apikey", "secret", "token", "passwd", "password", "credential", "credentials"}
)
_SECRET_SEGMENT_PAIRS = frozenset(
    {("api", "key"), ("auth", "key"), ("access", "key"), ("private", "key")}
)
_SEGMENT_SPLIT = re.compile(r"[_\-]+|(?<=[a-z0-9])(?=[A-Z])")

# Literals made of these markers are placeholders/docs, not secrets.
_PLACEHOLDER_MARKERS = (
    "your",
    "example",
    "placeholder",
    "changeme",
    "dummy",
    "redacted",
    "xxxx",
    "<",
    "${",
    "{{",
    "none",
    "null",
    "todo",
    "sample",
)


@dataclass(frozen=True, slots=True)
class Rule:
    """One detection rule: compiled pattern, finding message and optional line skip."""

    rule_id: str
    pattern: re.Pattern[str]
    message: str
    skip_if: re.Pattern[str] | None = None


def _compile(pattern: str) -> re.Pattern[str]:
    """Compile one rule pattern with insignificant whitespace ignored."""
    return re.compile(pattern, re.VERBOSE)


_RULE_SPECS: tuple[tuple[str, str, str, re.Pattern[str] | None], ...] = (
    (
        "hardcoded-secret-assignment",
        rf"""
        (?i)                                   # DEEPSEEK_API_KEY / api_key / ApiKey
        (?<![\w"'])                            # not mid-token / after a quote
        (?P<name>{_SECRET_NAME})               # NAME carrying a secret word
        \s*(?::\s*[A-Za-z_][\w\[\]|., ]*)?    # optional annotation: str | None
        \s*=\s*
        {_SECRET_LITERAL}
        """,
        "hardcoded secret assigned to a string literal",
        None,
    ),
    (
        "hardcoded-secret-json",
        rf"""
        (?i)                                   # "API_KEY" / "api_key" / "ApiKey"
        (?P<key_quote>["'])(?P<name>{_SECRET_NAME})(?P=key_quote)   # quoted key
        \s*:\s*
        {_SECRET_LITERAL}
        """,
        "hardcoded secret stored under a quoted key",
        None,
    ),
    (
        "hardcoded-secret-prefix",
        r"""
        (?P<secret>
            \b(?:
                  sk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{16,}   # OpenAI/Anthropic
                | gh[pousr]_[A-Za-z0-9]{20,}                         # GitHub tokens
                | github_pat_[A-Za-z0-9_]{20,}                       # GitHub PAT
                | xox[baprs]-[A-Za-z0-9-]{10,}                       # Slack tokens
                | AKIA[0-9A-Z]{16}                                   # AWS access key
                | AIza[0-9A-Za-z_-]{35}                              # Google API key
                | Bearer\s+[A-Za-z0-9\-._~+/]{20,}                  # literal bearer
            )
        )
        """,
        "known credential prefix inside a literal",
        None,
    ),
    (
        "private-key-material",
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----",
        "embedded private key material",
        None,
    ),
    ("dangerous-eval", r"(?<![\w.])eval\s*\(", "eval() on potentially untrusted input", None),
    ("dangerous-exec", r"(?<![\w.])exec\s*\(", "exec() on potentially untrusted input", None),
    ("dangerous-shell-true", r"\bshell\s*=\s*True\b", "subprocess call with shell=True", None),
    ("dangerous-verify-false", r"\bverify\s*=\s*False\b", "TLS verification disabled", None),
    (
        "dangerous-pickle-load",
        r"\bpickle\.loads?\s*\(",
        "pickle deserialization of untrusted data",
        None,
    ),
    ("dangerous-os-system", r"\bos\.system\s*\(", "os.system() shell invocation", None),
    (
        "dangerous-yaml-load",
        r"\byaml\.load\s*\(",
        "yaml.load() without SafeLoader",
        re.compile(r"SafeLoader"),
    ),
)

_RULES: tuple[Rule, ...] = tuple(
    Rule(rule_id, _compile(pattern), message, skip_if)
    for rule_id, pattern, message, skip_if in _RULE_SPECS
)


@dataclass(frozen=True, slots=True)
class Hit:
    """One rule match: identified location, raw snippet and printable form."""

    rule_id: str
    message: str
    rel_path: str
    line: int
    snippet: str
    display: str


@dataclass(frozen=True, slots=True)
class BaselineEntry:
    """One reviewed exemption, keyed by rule + path + snippet fingerprint."""

    rule_id: str
    rel_path: str
    fingerprint: str
    reason: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Outcome of one scan: un-baselined hits, exempted hits, stale rows."""

    violations: tuple[Hit, ...]
    allowed: tuple[Hit, ...]
    stale: tuple[BaselineEntry, ...]


def fingerprint(snippet: str) -> str:
    """Return the stable, secret-free baseline key for a matched snippet."""
    return hashlib.sha256(" ".join(snippet.split()).encode("utf-8")).hexdigest()[:16]


def _is_secret_name(name: str) -> bool:
    """Return True when an identifier really is secret-bearing (segment-level match)."""
    segments = [segment.lower() for segment in _SEGMENT_SPLIT.split(name) if segment]
    if any(segment in _SECRET_SEGMENTS for segment in segments):
        return True
    return any(pair in _SECRET_SEGMENT_PAIRS for pair in zip(segments, segments[1:], strict=False))


def _looks_like_non_secret(name: str, value: str) -> bool:
    """Return True for secret-ish names whose literal is config/documentation.

    ``max_tokens`` style keys are token *counts*, and help/description strings
    are prose; neither is a credential. Credentials are single tokens with no
    whitespace and no ``tokens``-plural name.
    """
    lowered = value.lower()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        return True
    if len(set(value)) <= 2:
        return True
    if any(char.isspace() for char in value):
        return True
    return name.lower().endswith(("tokens", "token_count"))


def load_baseline(path: Path) -> tuple[BaselineEntry, ...]:
    """Parse the TSV baseline file; raise ValueError on malformed rows."""
    if not path.exists():
        return ()
    entries: list[BaselineEntry] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = [field.strip() for field in line.split("\t")]
        if len(fields) != 4 or not all(fields):
            raise ValueError(f"{path.name}:{lineno}: expected 4 tab-separated fields")
        entries.append(BaselineEntry(fields[0], fields[1], fields[2], fields[3]))
    return tuple(entries)


def collect_files(targets: Iterable[Path]) -> list[Path]:
    """Expand targets into the scan set; missing paths (deletions) are skipped."""
    files: set[Path] = set()
    for target in targets:
        if not target.exists():
            continue
        if target.is_dir():
            files.update(
                path
                for path in target.rglob("*")
                if path.is_file()
                and path.suffix in SOURCE_SUFFIXES
                and "__pycache__" not in path.parts
            )
        elif target.suffix in SOURCE_SUFFIXES:
            files.add(target)
    return sorted(files)


def scan_files(
    files: Iterable[Path],
    repo_root: Path,
    rules: Sequence[Rule] = _RULES,
) -> list[Hit]:
    """Scan source files line by line and return every rule hit (no baseline applied)."""
    hits: list[Hit] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel_path = Path(os.path.relpath(path, repo_root)).as_posix()
        for lineno, line in enumerate(text.splitlines(), start=1):
            for rule in rules:
                if rule.skip_if is not None and rule.skip_if.search(line):
                    continue
                match = rule.pattern.search(line)
                if match is None:
                    continue
                groups = match.groupdict()
                secret = groups.get("secret")
                name = groups.get("name")
                if secret is not None and name is not None:
                    if not _is_secret_name(name):
                        continue
                    if _looks_like_non_secret(name, secret):
                        continue
                snippet = match.group(0)
                display = (
                    snippet.replace(secret, f"<redacted {len(secret)} chars>")
                    if secret is not None
                    else snippet
                )
                hits.append(Hit(rule.rule_id, rule.message, rel_path, lineno, snippet, display))
    return hits


def run_scan(
    files: Iterable[Path],
    repo_root: Path,
    baseline: Sequence[BaselineEntry],
) -> ScanResult:
    """Classify every hit against the explicit baseline allowlist."""
    allowed_keys = {(entry.rule_id, entry.rel_path, entry.fingerprint) for entry in baseline}
    used_keys: set[tuple[str, str, str]] = set()
    violations: list[Hit] = []
    allowed: list[Hit] = []
    for hit in scan_files(files, repo_root):
        key = (hit.rule_id, hit.rel_path, fingerprint(hit.snippet))
        if key in allowed_keys:
            allowed.append(hit)
            used_keys.add(key)
        else:
            violations.append(hit)
    stale = tuple(
        entry
        for entry in baseline
        if (entry.rule_id, entry.rel_path, entry.fingerprint) not in used_keys
    )
    return ScanResult(tuple(violations), tuple(allowed), stale)


def _format_violation(hit: Hit) -> str:
    """Render one violation as path:line: rule: message (fingerprint for the baseline)."""
    fp = fingerprint(hit.snippet)
    return f"{hit.rel_path}:{hit.line}: {hit.rule_id}: {hit.message}: {hit.display} [fp {fp}]"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the scan; return 0 when clean, 1 on un-baselined hits, 2 on a bad baseline."""
    raw_args = list(sys.argv[1:] if argv is None else argv)
    targets = [Path(arg).resolve() for arg in raw_args] or list(VENDORED_ROOTS)
    try:
        baseline = load_baseline(BASELINE_PATH)
    except ValueError as exc:
        sys.stderr.write(f"vendored security scan: {exc}\n")
        return 2

    files = collect_files(targets)
    if not files:
        sys.stdout.write("vendored security scan: no files to scan\n")
        return 0

    result = run_scan(files, REPO_ROOT, baseline)
    for hit in result.violations:
        sys.stdout.write(_format_violation(hit) + "\n")
    if result.violations:
        sys.stdout.write(
            f"\nvendored security scan: {len(result.violations)} un-baselined hit(s).\n"
            f"Review each hit; exempt confirmed-safe ones in {BASELINE_PATH.name} with "
            "the printed fingerprint and a concrete reason.\n"
        )
        return 1

    for entry in result.stale:
        sys.stdout.write(
            "vendored security scan: stale baseline row (matches no hit, delete it): "
            f"{entry.rule_id} {entry.rel_path} {entry.fingerprint}\n"
        )
    sys.stdout.write(
        f"vendored security scan: clean ({len(files)} file(s), {len(result.allowed)} baselined)\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
