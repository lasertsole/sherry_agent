"""Build the optional CUDA pack for the desktop release (packing-plan B2).

    uv run --with pyinstaller python packaging/build_cuda_pack.py     # Linux ONLY

Produces ``client/src-tauri/sherry-cuda-pack/`` — the CUDA-only payload the
unified CPU installer leaves OUT: the ``nvidia-*`` wheel libraries, ``triton``
and torch's own CUDA binaries (``libtorch_cuda*``). The tree's CONTENTS become
the release asset ``sherry-cuda-pack-linux-x86_64.tar.gz``, which the desktop
shell downloads on the user's explicit opt-in from the model settings tab and
injects into the backend's loader search path at spawn time
(``ld-linux --library-path``, see
``client/src-tauri/src/services/cuda_pack_downloader.rs`` and
``python_process.rs``).

The ``VERSION`` marker records the BACKEND version this pack was built from
(the same ``tauri.conf.json`` field the backend tree carries): one release
pipeline builds both, so matching backend versions transitively pin the
matching torch ABI — the shell refuses to load a pack whose marker differs.

The pack is Linux-only by construction: the lock's CUDA dependencies are all
``sys_platform == 'linux'`` markers, so Windows/macOS wheels never carry CUDA
in the first place (a guard below refuses to run elsewhere). The script must
run against the venv as the lock resolved it — a CPU-swapped torch is refused
via ``torch.version.cuda``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_backend as bb  # the shared PyInstaller recipe (same lock, same flags)

ROOT = bb.ROOT
PACK_DIR = bb.DIST_DIR / "sherry-cuda-pack"
WORK_DIR = ROOT / "build" / "pyinstaller-cuda"

#: The CUDA payload: directories copied WHOLE from the built ``_internal`` tree
#: (preserving torch's relative layout), plus torch's own CUDA binaries.
HARVEST_DIRS = ("nvidia", "triton")
HARVEST_GLOBS = ("torch/lib/*cuda*",)


def _require_cuda_torch() -> None:
    """Refuse to build a (useless, multi-GiB) pack against a CPU torch."""
    import torch

    if not torch.version.cuda:
        print(
            "refusing: this venv's torch has no CUDA support — run without the "
            "CPU-torch swap (uv sync --frozen on Linux resolves the CUDA wheels)",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _build_tree() -> Path:
    """PyInstaller onedir with the CUDA torch resolved venv, into a private dir."""
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
        str(WORK_DIR),
        "--workpath",
        str(WORK_DIR),
        "--specpath",
        str(WORK_DIR),
        "--log-level",
        "WARN",
    ]
    for source, dest in bb.DATA:
        import os

        args += ["--add-data", f"{ROOT / source}{os.pathsep}{dest}"]
    for package in bb.COLLECT_ALL:
        args += ["--collect-all", package]
    for package in bb.COLLECT_SUBMODULES:
        args += ["--collect-submodules", package]
    for module in bb.EXCLUDES:
        args += ["--exclude-module", module]
    args.append(str(ROOT / "packaging" / "backend_entry.py"))

    print("building the CUDA backend tree:", " ".join(args[:12]), "…")
    result = subprocess.run(args, cwd=ROOT, check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    return WORK_DIR / "sherry-backend" / "_internal"


def _harvest(internal: Path, pack: Path) -> int:
    """Copy the CUDA payload into the pack, preserving relative layout."""
    files = 0
    for name in HARVEST_DIRS:
        source = internal / name
        if not source.is_dir():
            print(f"warning: no {name}/ in the built tree (torch layout changed?)", file=sys.stderr)
            continue
        shutil.copytree(source, pack / name)
    for pattern in HARVEST_GLOBS:
        for source in internal.glob(pattern):
            if source.is_file():
                dest = pack / source.relative_to(internal)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
    for so in pack.rglob("*"):
        if so.is_file():
            files += 1
    return files


def main() -> int:
    if sys.platform != "linux":
        print(
            "refusing: the CUDA pack is Linux-only (the lock's CUDA wheels are linux-marked)",
            file=sys.stderr,
        )
        return 1
    _require_cuda_torch()

    if PACK_DIR.exists():
        shutil.rmtree(PACK_DIR)
    PACK_DIR.mkdir(parents=True, exist_ok=True)

    internal = _build_tree()
    files = _harvest(internal, PACK_DIR)
    # The multi-GiB CUDA backend tree was only a harvester: drop it.
    shutil.rmtree(WORK_DIR, ignore_errors=True)

    if not files:
        print("refusing: nothing was harvested — the pack would be empty", file=sys.stderr)
        return 1
    version = bb.installer_version()
    bb.write_version_marker(PACK_DIR, version)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from check_release import audit_backend  # local import: this file is a script

    leaks = audit_backend(PACK_DIR)
    if leaks:
        print("refusing: the pack contains tests/evals/docs/wiki payload:", file=sys.stderr)
        for leak in leaks[:50]:
            print(f"  {leak}", file=sys.stderr)
        return 1

    total = sum(f.stat().st_size for f in PACK_DIR.rglob("*") if f.is_file())
    print(f"ok: {PACK_DIR} ({total / (1024**3):.2f} GiB, {files} files, VERSION={version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
