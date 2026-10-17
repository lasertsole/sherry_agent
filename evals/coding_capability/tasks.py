"""Coding tasks for the coding-capability baseline eval suite.

Each task is a throwaway git repository built under the run's results directory.
Its tests fail until the agent changes the code, and the verdict is produced by
running that repository's OWN test suite — objective, offline, no LLM judge and
no external benchmark (see ``TODO/对标计划_编码能力实测方案.md`` §六 for the
reporting discipline this shape enforces).

Two harness artifacts are deliberately kept out of the measurement:

- every repo carries a root ``conftest.py`` that puts the repo root on
  ``sys.path``, so ``pytest`` behaves the same however it is invoked (a bare
  ``pytest`` would otherwise miss the top-level modules and make a correct fix
  look like a failure);
- the passing state of each task is verified by the harness itself when the
  suite starts (``verify_reference_fix``), with the reference fix applied to a
  scratch copy: a task whose tests do not pass with the intended fix is a
  harness bug, not a model failure.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: Every repo's conftest keeps the repo root importable under any pytest
#: invocation (`pytest` vs `python -m pytest`).
_CONFTEST = """import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
"""

#: Injected into every task repo (before the task's own files, which may override).
_CONFTEST_FILES = {"conftest.py": _CONFTEST}


@dataclass(frozen=True)
class Task:
    """One coding task: a repository skeleton, a prompt, and its verdict."""

    id: str
    prompt: str
    files: dict[str, str]
    #: The intended fix, applied to a scratch copy to prove the task is solvable.
    reference_fix: dict[str, str]
    timeout_s: float = 480.0


TASKS: tuple[Task, ...] = (
    Task(
        id="slugify-bugfix",
        prompt=(
            "The test suite in this repository is failing. Fix the implementation so "
            "that `python -m pytest -q` passes. The tests describe the expected "
            "behaviour; do not change them."
        ),
        files={
            "textkit/__init__.py": "",
            "textkit/slugify.py": '''"""Slug helper for the textkit package."""


def slugify(text: str) -> str:
    """Return a lowercase, hyphen-separated slug for *text*.

    Every run of non-alphanumeric characters collapses into a single hyphen,
    leading and trailing hyphens are dropped, and letters are lowercased.
    Unicode letters and digits are kept as-is.
    """
    out = []
    for ch in text:
        if ch.isalnum():
            out.append(ch)
        else:
            out.append("-")
    return "".join(out)
''',
            "tests/test_slugify.py": """from textkit.slugify import slugify


def test_lowercases_words():
    assert slugify("Hello World") == "hello-world"


def test_collapses_separator_runs():
    assert slugify("a  --  b") == "a-b"


def test_strips_edges():
    assert slugify("  Hello! ") == "hello"


def test_keeps_digits_and_unicode_letters():
    assert slugify("V2 Räkna") == "v2-räkna"
""",
        },
        reference_fix={
            "textkit/slugify.py": '''"""Slug helper for the textkit package."""

import re


def slugify(text: str) -> str:
    """Return a lowercase, hyphen-separated slug for *text*."""
    return re.sub(r"[^0-9a-z\\u00c0-\\uffff]+", "-", text.lower()).strip("-")
''',
        },
    ),
    Task(
        id="duration-feature",
        prompt=(
            "This repository has a documented function that is not implemented yet: "
            "`parse_duration` raises NotImplementedError. Implement it exactly as the "
            "docstring and the tests require, so that `python -m pytest -q` passes. "
            "Do not change the tests."
        ),
        files={
            "duration.py": '''"""Duration parsing helper."""


def parse_duration(text: str) -> int:
    """Parse a duration such as "1h30m", "45s" or "2m10s" into whole seconds.

    Components use the units ``h``, ``m`` and ``s``, each at most once, may
    appear in any order, carry no space between a number and its unit, and are
    not separated. Any other input raises ``ValueError`` — including an empty
    string, a bare number without a unit, and a repeated unit.
    """
    raise NotImplementedError
''',
            "tests/test_duration.py": """import pytest

from duration import parse_duration


def test_single_units():
    assert parse_duration("45s") == 45
    assert parse_duration("2m") == 120
    assert parse_duration("3h") == 10800


def test_combined_units_any_order():
    assert parse_duration("1h30m") == 5400
    assert parse_duration("30m1h") == 5400
    assert parse_duration("1h30m15s") == 5415


def test_rejects_bad_input():
    for bad in ("", "45", "m", "1h1h", "1x", "1 h", "1h30"):
        with pytest.raises(ValueError):
            parse_duration(bad)
""",
        },
        reference_fix={
            "duration.py": '''"""Duration parsing helper."""

import re

_UNITS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text: str) -> int:
    """Parse a duration such as "1h30m", "45s" or "2m10s" into whole seconds."""
    if not text:
        raise ValueError(f"invalid duration: {text!r}")
    parts = re.findall(r"(\\d+)([hms])", text)
    if not parts or "".join(f"{number}{unit}" for number, unit in parts) != text:
        raise ValueError(f"invalid duration: {text!r}")
    total = 0
    seen: set[str] = set()
    for number, unit in parts:
        if unit in seen:
            raise ValueError(f"repeated unit: {unit}")
        seen.add(unit)
        total += int(number) * _UNITS[unit]
    return total
''',
        },
    ),
    Task(
        id="cart-multifile",
        prompt=(
            "This repository has an unimplemented pricing helper: `money.apply_discount` "
            "raises NotImplementedError. Implement it exactly as documented and wire "
            "`cart.total` through it (the cart must apply the discount to the summed "
            "subtotal, and its tests must pass too). `python -m pytest -q` must pass; "
            "do not change the tests."
        ),
        files={
            "money.py": '''"""Money helpers (amounts are whole cents)."""


def apply_discount(cents: int, percent: float) -> int:
    """Return *cents* reduced by *percent*, rounded HALF-UP to whole cents.

    ``percent`` is a percentage (10 means 10 percent). Percentages above 100
    are refused with ``ValueError``.
    """
    raise NotImplementedError
''',
            "cart.py": '''"""Shopping cart totals."""

from money import apply_discount


def total(lines: list[tuple[str, int, int]], discount_percent: float = 0) -> int:
    """Sum ``(sku, unit_cents, qty)`` lines, then apply *discount_percent*."""
    subtotal = sum(unit_cents * qty for _sku, unit_cents, qty in lines)
    if discount_percent == 0:
        return subtotal
    return apply_discount(subtotal, discount_percent)
''',
            "tests/test_money.py": """import pytest

from money import apply_discount


def test_rounds_half_up():
    assert apply_discount(1000, 12.5) == 875
    assert apply_discount(101, 5) == 96


def test_refuses_above_one_hundred():
    with pytest.raises(ValueError):
        apply_discount(100, 101)
""",
            "tests/test_cart.py": """from cart import total


def test_subtotal_without_discount():
    assert total([("a", 250, 2), ("b", 100, 1)]) == 600


def test_discount_applies_to_the_subtotal():
    assert total([("a", 250, 2), ("b", 100, 1)], discount_percent=10) == 540
""",
        },
        reference_fix={
            "money.py": '''"""Money helpers (amounts are whole cents)."""

from decimal import ROUND_HALF_UP, Decimal


def apply_discount(cents: int, percent: float) -> int:
    """Return *cents* reduced by *percent*, rounded HALF-UP to whole cents."""
    if percent > 100:
        raise ValueError(f"refusing a discount above 100%: {percent}")
    reduced = Decimal(cents) * (Decimal(100) - Decimal(str(percent))) / Decimal(100)
    return int(reduced.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
''',
        },
    ),
    Task(
        id="wordcount-tiebreak",
        prompt=(
            "The word-count helper in this repository returns the wrong results. Fix "
            "`top_words` so that `python -m pytest -q` passes: the tests pin both the "
            "number of entries and the tie-breaking order, and they cover the "
            "`top <= 0` case. Do not change the tests."
        ),
        files={
            "wc_cli/__init__.py": "",
            "wc_cli/words.py": '''"""Word counting for the wc_cli tool."""

from collections import Counter


def count_words(text: str) -> Counter:
    """Count lowercase words, splitting on whitespace."""
    return Counter(word.lower() for word in text.split())


def top_words(counts: Counter, top: int) -> list[tuple[str, int]]:
    """The *top* most common ``(word, count)`` pairs.

    Equal counts are ordered alphabetically by word. ``top`` is the exact
    number of pairs to return; ``top <= 0`` means "all of them".
    """
    return counts.most_common(top + 1)
''',
            "tests/test_words.py": """from wc_cli.words import count_words, top_words

TEXT = "the cat the dog the bird a cat a bird"


def test_counts():
    counts = count_words(TEXT)
    assert counts["the"] == 3
    assert counts["cat"] == 2


def test_returns_exactly_top_entries():
    counts = count_words(TEXT)
    assert [word for word, _ in top_words(counts, 2)] == ["the", "a"]


def test_ties_break_alphabetically():
    counts = count_words(TEXT)
    assert [word for word, _ in top_words(counts, 3)] == ["the", "a", "bird"]


def test_non_positive_top_means_all():
    counts = count_words(TEXT)
    assert [word for word, _ in top_words(counts, 0)] == ["the", "a", "bird", "cat", "dog"]
""",
        },
        reference_fix={
            "wc_cli/words.py": '''"""Word counting for the wc_cli tool."""

from collections import Counter


def count_words(text: str) -> Counter:
    """Count lowercase words, splitting on whitespace."""
    return Counter(word.lower() for word in text.split())


def top_words(counts: Counter, top: int) -> list[tuple[str, int]]:
    """The *top* most common ``(word, count)`` pairs."""
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ranked if top <= 0 else ranked[:top]
''',
        },
    ),
)


def build_repo(task: Task, repo_dir: Path) -> str:
    """Materialise the task as a git repository with ONE base commit.

    Returns the base commit SHA: the harness reports diffs relative to it (the
    measurement rule is a diff against the pinned base, never against a moving
    working copy), and the agent is expected to leave the history alone.
    """
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    repo_dir.mkdir(parents=True)
    files = {**_CONFTEST_FILES, **task.files}
    for relative, content in files.items():
        target = repo_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo_dir, "init", "-q", "-b", "main")
    _git(repo_dir, "config", "user.email", "evals@sherry.local")
    _git(repo_dir, "config", "user.name", "sherry-evals")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "base")
    return _git(repo_dir, "rev-parse", "HEAD").strip()


def run_tests(repo_dir: Path, timeout_s: float = 120.0) -> tuple[int, str]:
    """Run the repo's own suite; returns ``(exit_code, tail_of_output)``.

    ``python -m pytest`` (not the ``pytest`` script) so the repo root is on
    ``sys.path`` regardless of how the agent invoked it, and exit code 5
    ("no tests ran") is NOT a pass.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    output = (completed.stdout + completed.stderr).strip()
    return completed.returncode, output[-2000:]


def git_diff(repo_dir: Path, base_sha: str) -> tuple[str, str]:
    """``(stat, full diff)`` of the working tree against the pinned base."""
    stat = _git(repo_dir, "diff", "--stat", base_sha).strip()
    full = _git(repo_dir, "diff", base_sha).strip()
    return stat, full


def changed_paths(repo_dir: Path, base_sha: str) -> list[str]:
    """Repo-relative paths the working tree changed against the pinned base."""
    listed = _git(repo_dir, "diff", "--name-only", base_sha).strip()
    return [line.strip() for line in listed.splitlines() if line.strip()]


#: The tests ARE the task's definition, so a "fix" that edits them is not a fix
#: (a green suite reached that way is scored as a failure, with the flag set).
PROTECTED_DIRS = ("tests/",)
PROTECTED_FILES = ("conftest.py",)


def tampered_paths(changed: list[str]) -> list[str]:
    """The changed paths that fall inside the task's protected test surface."""
    return [
        path
        for path in changed
        if path in PROTECTED_FILES or any(path.startswith(d) for d in PROTECTED_DIRS)
    ]


def commit_count(repo_dir: Path) -> int:
    """Commits on HEAD — the task rule is "no commits in the task repo"."""
    return int(_git(repo_dir, "rev-list", "--count", "HEAD").strip() or "0")


def _git(repo_dir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout


def verify_reference_fix(task: Task) -> tuple[bool, str]:
    """Apply the intended fix to a scratch copy and prove the tests pass.

    A task that fails this check is a broken TASK (harness bug) — the suite
    reports it separately so "the model failed" and "the harness failed" never
    get confused (the plan's exit criterion 1).
    """
    with tempfile.TemporaryDirectory(prefix=f"eval-task-{task.id}-") as tmp:
        repo = Path(tmp) / task.id
        build_repo(task, repo)
        for relative, content in task.reference_fix.items():
            (repo / relative).write_text(content, encoding="utf-8")
        code, output = run_tests(repo)
        return code == 0, output
