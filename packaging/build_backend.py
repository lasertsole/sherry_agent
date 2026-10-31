"""Build the desktop backend tree (PyInstaller onedir) for the release archive.

    uv run --with pyinstaller python packaging/build_backend.py

Writes ``client/src-tauri/sherry-backend/`` — the executable, its ``_internal``
tree, the shipped data files and a ``VERSION`` marker. That directory's
CONTENTS become the per-platform release archive
(``sherry-backend-<os>-<arch>.tar.gz|.zip``, built by
``.github/workflows/desktop-build.yml``), which the desktop shell downloads on
its first launch into ``<app_home>/sherry-backend/`` and spawns (see
``client/src-tauri/src/services/backend_downloader.rs``).

The ``VERSION`` marker is what keeps that download to once per release: it holds
the installer's own version (``client/src-tauri/tauri.conf.json``, the field the
shell reports as ``package_info().version``), and a tree whose marker differs is
re-downloaded. Do not derive it from a git tag or a branch name — the shell
compares it against the version it was built with.

ONEDIR on purpose: the dependency tree is measured in gigabytes, so a onefile
build would unpack it on every launch.

Per-OS by construction — PyInstaller cannot cross-compile: run this on each
target (Linux/Windows/macOS) right before ``tauri build`` there.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = ROOT / "client" / "src-tauri"
APP_DIR = DIST_DIR / "sherry-backend"
WORK_DIR = ROOT / "build" / "pyinstaller"
TAURI_CONF = DIST_DIR / "tauri.conf.json"

#: Ship data the backend reads from the code tree (the rest of the runtime tree
#: is user data and lives in SHERRY_HOME). ``source:dest`` pairs, relative to the
#: repo root; dest paths are where the loader/dir config expects them —
#: ``skills/builtin`` and ``workspace/template`` are read by ``config/path``
#: (ROOT_DIR = the bundle root), so the bundle must mirror the checkout layout.
DATA = (
    ("skills", "skills"),  # builtin playbooks (auto/plugins live in APP_HOME)
    ("workspace/template", "workspace/template"),  # persona seeds for first run
    ("plugins", "plugins"),  # channel adapters
    ("sherry.jsonc", "sherry.jsonc"),
    # The tracked .env (non-secret app config) and the two config templates a
    # first run seeds into APP_HOME as .models / .network.
    (".env", ".env"),
    (".models.example", ".models.example"),
    (".network.example", ".network.example"),
    ("context_engine", "context_engine"),  # read by path, not imported
)

#: Packages whose data files PyInstaller's built-in hooks miss.
COLLECT_ALL = ("robyn", "sniffio", "httpx", "websockets", "onnxruntime")

#: Imported LAZILY inside a function (the load gate's sampler), so the freezer's
#: static analysis cannot see them from the entry point. ``psutil`` also ships a
#: per-platform C extension, which a missing hidden import would break at
#: runtime rather than at build time — the desktop workflow's packaging smoke
#: step imports and calls it in the frozen tree for exactly that reason.
HIDDEN_IMPORTS = ("psutil",)

#: OUR packages, collected whole. They resolve parts of themselves through
#: ``importlib.import_module`` (``models/__init__`` and ``models/LLMs/__init__``
#: expose lazy attributes that way, and the tool packages build tools lazily), so
#: the freezer cannot see every module from the entry point alone — the first
#: frozen runs died on ``No module named 'models.LLMs.auxiliary_llm'``.
COLLECT_SUBMODULES = (
    "agent",
    "bus",
    "channels",
    "config",
    "context_engine",
    "logs",
    "models",
    "plugins",
    "pub",
    "runtime",
    "server",
    "skills",
    "workspace",
)

#: Static analysers that only a subagent feature needs; excluding them keeps the
#: bundle from dragging every tree-sitter grammar's sources in twice. ``tests``
#: and ``evals`` are also excluded so PyInstaller can never pull the repository's
#: suites into a release.
EXCLUDES = (
    "tkinter",
    "matplotlib",
    "IPython",
    "jupyter",
    "pytest",
    "tests",
    "evals",
    "pandas.tests",
)


def installer_version() -> str:
    """The version the Tauri installer reports — ``tauri.conf.json``'s ``version``.

    The shell re-downloads the backend when this differs from the tree's
    ``VERSION`` marker, and the value it compares against is the config the
    installer was built from, so the marker must come from the same field.
    """
    data = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    version = str(data.get("version", "")).strip()
    return version or "0.0.0"


def write_version_marker(app_dir: Path, version: str | None = None) -> Path:
    """Write ``VERSION`` into the built tree and return its path."""
    marker = app_dir / "VERSION"
    marker.write_text(f"{version or installer_version()}\n", encoding="utf-8")
    return marker


def main() -> int:
    # The directory is a generated artifact: wipe it (PyInstaller's --noconfirm
    # removes it anyway) and re-copy the tracked README afterwards — the doc's
    # home is this directory's README.md, next to the scripts it documents.
    if APP_DIR.is_symlink():
        print(f"refusing: {APP_DIR} is a symlink", file=sys.stderr)
        return 1
    if APP_DIR.exists():
        shutil.rmtree(APP_DIR)
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    # A symlinked resource target is a data-loss trap: ``tauri-build``'s
    # build-time resource copy truncates each destination before reading its
    # source, so ``target/release/sherry-backend -> here`` makes it empty this
    # tree file by file (observed once: 7,634 zero-byte files).
    stale_link = DIST_DIR / "target" / "release" / "sherry-backend"
    if stale_link.is_symlink():
        stale_link.unlink()
        print(f"removed stale symlink {stale_link} — it would truncate the tree")

    args: list[str] = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        "sherry-backend",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(WORK_DIR),
        "--specpath",
        str(WORK_DIR),
        "--log-level",
        "WARN",
    ]
    for source, dest in DATA:
        args += ["--add-data", f"{ROOT / source}{__import__('os').pathsep}{dest}"]
    for package in COLLECT_ALL:
        args += ["--collect-all", package]
    for package in COLLECT_SUBMODULES:
        args += ["--collect-submodules", package]
    for module in HIDDEN_IMPORTS:
        args += ["--hidden-import", module]
    for module in EXCLUDES:
        args += ["--exclude-module", module]
    args.append(str(ROOT / "packaging" / "backend_entry.py"))

    print("building:", " ".join(args[:12]), "…")
    result = subprocess.run(args, cwd=ROOT, check=False)
    if result.returncode != 0:
        return result.returncode

    exe = APP_DIR / ("sherry-backend.exe" if sys.platform == "win32" else "sherry-backend")
    if not exe.is_file():
        print(f"expected the executable at {exe}", file=sys.stderr)
        return 1
    # The version marker the shell compares against its own build version.
    version = installer_version()
    write_version_marker(APP_DIR, version)
    # Ship the doc with the artifact (PyInstaller wiped the directory).
    shutil.copy2(ROOT / "packaging" / "README.md", APP_DIR / "README.md")
    # Release hygiene: the frozen tree must not carry the repo's test suite,
    # docs, or generated wiki. The same guard runs in the desktop workflow for
    # the SPA; see packaging/check_release.py.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from check_release import audit_backend  # local import: this file is a script

    leaks = audit_backend(APP_DIR)
    if leaks:
        print("refusing: the release tree contains tests/evals/docs/wiki payload:", file=sys.stderr)
        for leak in leaks[:50]:
            print(f"  {leak}", file=sys.stderr)
        if len(leaks) > 50:
            print(f"  … and {len(leaks) - 50} more", file=sys.stderr)
        return 1
    total = sum(f.stat().st_size for f in APP_DIR.rglob("*") if f.is_file())
    print(f"ok: {exe} ({total / (1024**3):.2f} GiB, VERSION={version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
