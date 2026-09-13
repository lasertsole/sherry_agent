"""SkillSpector scan cache: content-addressed key computation + version probes.

Scans are expensive, but their verdict depends only on: the skill's file
contents, the scanner version, the LLM mode and the backend. Those four
inputs are folded into a content-addressed key (NO filesystem path: uploads
land in random staging dirs, and identical content must share a verdict).
Version probing is memoised per backend id; every probe fails open so a
broken probe never blocks or slows a scan.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from config.features import SKILL_SCANNER
from loguru import logger

#: Memoised scanner-version fingerprints per backend id.
_VERSION_FINGERPRINT_CACHE: dict[str, str] = {}

#: How long (seconds) the ``--version`` probe of the CLI may run.
_VERSION_PROBE_TIMEOUT = SKILL_SCANNER["version_probe_timeout"]

#: On-disk schema version of the cache file.
_CACHE_VERSION = SKILL_SCANNER["cache_version"]


def _directory_content_hash(directory: Path) -> str:
    """Content-addressed hash of every file under *directory*.

    Walks the tree in sorted order and chains, per file, its POSIX-relative
    path and the sha256 of its bytes into one final sha256 hex digest, so any
    content change (rename, edit, added file) changes the digest.
    ``__pycache__`` dirs and ``*.pyc`` files are skipped (interpreter noise).
    A nonexistent directory hashes like an empty one (the loop never runs, so
    the result equals the sha256 of empty bytes) and never raises.
    """
    overall = hashlib.sha256()
    if directory.is_dir():
        for root, dirs, files in os.walk(directory):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                if name.endswith(".pyc"):
                    continue
                full = Path(root) / name
                rel = full.relative_to(directory).as_posix()
                try:
                    file_hash = hashlib.sha256(full.read_bytes()).hexdigest()
                except OSError:
                    file_hash = "unreadable"  # deleted/raced mid-walk; stay deterministic
                overall.update(rel.encode("utf-8") + b"\0" + file_hash.encode("ascii"))
    return overall.hexdigest()


def _cli_version_fingerprint(exe: str) -> str:
    """Version string of the CLI binary, with an exe-staleness fallback.

    Probes ``<exe> --version`` with a short timeout; when the probe fails or
    yields nothing, falls back to ``<mtime>:<size>`` of the binary so a
    replaced CLI still invalidates cached verdicts. Never raises.
    """
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_VERSION_PROBE_TIMEOUT,
            check=False,
        )
        version = (proc.stdout or "").strip()
        first_line = version.splitlines()[0].strip() if version else ""
        if first_line:
            return first_line
    except Exception:  # noqa: S110
        pass
    try:
        st = os.stat(exe)
        return f"{st.st_mtime}:{st.st_size}"
    except OSError:
        return "unknown"


def _scanner_version_fingerprint(backend: str) -> str:
    """Memoised fingerprint of the scanner installation for *backend*.

    ``"python"`` backends are fingerprinted as ``"python-api"`` (the in-process
    API has no separate binary). ``"cli"`` is fingerprinted by its reported
    version string, falling back to binary staleness. Any probe error fails
    open to the bare backend id — the cache just becomes less precise, never
    wrong in a blocking way.
    """
    cached = _VERSION_FINGERPRINT_CACHE.get(backend)
    if cached is not None:
        return cached
    fingerprint = backend  # fail-open default
    try:
        if backend == "python":
            fingerprint = "python-api"
        elif backend == "cli":
            exe = shutil.which("skillspector")
            if exe:
                fingerprint = _cli_version_fingerprint(exe)
    except Exception as exc:
        logger.debug("SkillSpector version probe failed for {}: {}", backend, exc)
    _VERSION_FINGERPRINT_CACHE[backend] = fingerprint
    return fingerprint
