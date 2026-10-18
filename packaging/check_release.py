"""Release-artifact guard: tests, docs and wiki must never ship.

A desktop release has two halves:

* the **backend tree** — the PyInstaller onedir built by
  ``packaging/build_backend.py`` and published as a per-platform asset;
* the **installer** — the static SPA (``client/.output/public``) plus the Rust
  shell, built by ``pnpm tauri build``.

``build_backend.py`` runs :func:`audit_backend` over the frozen tree, and the
desktop workflow runs this CLI over both halves before uploading anything. The
check is deliberately path-based: a shipped ``tests/``, ``docs/`` or ``wiki/``
directory (or a colocated ``*.test.ts`` / ``test_*.py`` file) fails the build
instead of being silently uploaded.

Third-party payload under ``_internal`` is exempt from the file scan: a wheel
may legitimately contain its own tests, and those are not this repository's
test suite. The top-level directory names are still checked, so a copied
``tests/`` at the bundle root cannot hide there.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

#: Directory names that must not appear anywhere in a shipped tree. They are
#: matched case-insensitively; a hit is reported and not descended into.
FORBIDDEN_DIR_NAMES = frozenset(
    {
        "tests",
        "test",
        "docs",
        "doc",
        "wiki",
        "__tests__",
        "node_modules",
        "graphify-out",
    }
)

#: Names that must not exist at the artifact root (the repo's own trees).
FORBIDDEN_TOP_NAMES = FORBIDDEN_DIR_NAMES | frozenset(
    {
        "evals",
        "client",
        "TODO",
        ".github",
    }
)

#: Frontend test files use one of these suffixes; Python pytest files use the
#: ``test_`` prefix. The suffix ``_test.py`` is deliberately NOT flagged: the
#: multimodal_rag skill ships two documented manual import utilities named
#: ``rag_import*_test.py``; they are not test suites.
FRONTEND_TEST_SUFFIXES = (
    ".test.ts",
    ".test.tsx",
    ".test.js",
    ".test.jsx",
    ".test.mjs",
    ".test.cjs",
    ".test.vue",
    ".spec.ts",
    ".spec.tsx",
    ".spec.js",
    ".spec.jsx",
    ".spec.mjs",
    ".spec.cjs",
    ".spec.vue",
)

MARKDOWN_SUFFIXES = (".md", ".markdown")

#: Walk noise that carries no audit value (and can be large).
_SKIP_DIR_NAMES = frozenset({"__pycache__", ".git", ".venv", "venv"})


def _relative(path: Path, root: Path) -> str:
    """The POSIX path shown in a leak report."""
    return path.relative_to(root).as_posix()


def _walk(root: Path, *, skip_internal: bool) -> list[str]:
    """Return the test/doc/wiki payload found under ``root``.

    ``skip_internal`` skips PyInstaller's dependency payload (``_internal``)
    for the backend tree; its third-party tests are not the repo's suite.
    """
    leaks: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        for name in list(dirnames):
            lowered = name.lower()
            if skip_internal and name == "_internal":
                dirnames.remove(name)
                continue
            if lowered in _SKIP_DIR_NAMES:
                dirnames.remove(name)
                continue
            if lowered in FORBIDDEN_DIR_NAMES:
                leaks.append(_relative(here / name, root))
                dirnames.remove(name)
                continue
        for name in filenames:
            lowered = name.lower()
            if lowered.startswith("test_") and lowered.endswith(".py"):
                leaks.append(_relative(here / name, root))
            elif lowered.endswith(FRONTEND_TEST_SUFFIXES):
                leaks.append(_relative(here / name, root))
    return leaks


def audit_backend(tree: Path) -> list[str]:
    """Leaks in a frozen backend tree (tests/docs/wiki, frontend test files)."""
    if not tree.is_dir():
        raise FileNotFoundError(f"backend tree not found: {tree}")
    leaks = [child.name for child in tree.iterdir() if child.name.lower() in FORBIDDEN_TOP_NAMES]
    leaks += _walk(tree, skip_internal=True)
    return sorted(set(leaks))


def audit_frontend(dist: Path) -> list[str]:
    """Leaks in the generated SPA directory (tests/docs/wiki, Markdown files)."""
    if not dist.is_dir():
        raise FileNotFoundError(f"frontend dist not found: {dist}")
    leaks = [child.name for child in dist.iterdir() if child.name.lower() in FORBIDDEN_TOP_NAMES]
    leaks += _walk(dist, skip_internal=False)
    leaks += [
        _relative(path, dist)
        for path in dist.rglob("*")
        if path.is_file() and path.suffix.lower() in MARKDOWN_SUFFIXES
    ]
    return sorted(set(leaks))


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``check_release.py {backend|frontend} <path>``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("backend", "frontend"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)

    try:
        leaks = audit_backend(args.path) if args.kind == "backend" else audit_frontend(args.path)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if leaks:
        print(f"release leak: {args.kind} tree contains tests/docs/wiki payload:", file=sys.stderr)
        for leak in leaks[:50]:
            print(f"  {leak}", file=sys.stderr)
        if len(leaks) > 50:
            print(f"  … and {len(leaks) - 50} more", file=sys.stderr)
        return 1
    print(f"ok: {args.kind} tree has no test/doc/wiki payload ({args.path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
