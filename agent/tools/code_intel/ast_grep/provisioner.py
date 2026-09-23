"""ast-grep binary auto-provisioning — SHA-256 verified GitHub download.

Mirrors oh-my-openagent's sg-provisioner.ts:

  1. Download the pinned release ZIP from GitHub (bounded by
     ``ast_grep_provision_timeout_s``).
  2. SHA-256-verify the archive; a mismatch is fatal (fail-closed — never
     installs unverified bytes).
  3. Extract the standalone real binary from the ZIP using stdlib ``zipfile``
     (the host has no ``unzip``).
  4. Atomically write it to ``~/.sherry/runtime/ast-grep/<slug>/sg`` (chmod 755).

The release ships two entries: a large ``ast-grep`` binary and a small ``sg``
launcher that re-execs it relative to its own path. The launcher cannot run in
restricted PRoot sandboxes, so extraction always prefers ``ast-grep`` and writes
it under the ``sg`` name the resolver looks for.
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import zipfile
from pathlib import Path

from loguru import logger

from config.features import AST_GREP

from .resolver import _binary_name, _clear_cache_for_tests, _runtime_slug, runtime_dir


def _sha256(data: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of *data*."""
    return hashlib.sha256(data).hexdigest()


def _download(url: str, timeout_s: float) -> bytes:
    """Download *url* and return its bytes, bounded by *timeout_s*."""
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "sherry-agent"})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return response.read()


def _extract_binary(zip_bytes: bytes, platform: str) -> bytes:
    """Extract the standalone binary from a release ZIP.

    Prefers ``ast-grep`` over the ``sg`` launcher regardless of the archive
    entry order; the launcher re-execs relative to its own path and cannot run
    in every sandbox.
    """
    suffix = ".exe" if platform == "win32" else ""
    preferred_names = [f"ast-grep{suffix}", f"sg{suffix}"]
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        by_basename = {name.split("/")[-1]: name for name in archive.namelist()}
        for preferred in preferred_names:
            if preferred in by_basename:
                return archive.read(by_basename[preferred])
    raise FileNotFoundError(f"No standalone {' or '.join(preferred_names)} binary in ZIP")


def _write_atomic(destination: Path, payload: bytes) -> None:
    """Write *payload* to *destination* atomically with mode 755 on POSIX."""
    tmp_path = destination.with_name(destination.name + ".tmp")
    tmp_path.write_bytes(payload)
    if sys.platform != "win32":
        os.chmod(tmp_path, 0o755)
    os.replace(tmp_path, destination)


def provision_sg_binary() -> str | None:
    """Download, verify, and install the pinned ast-grep binary.

    Returns the installed binary path on success, or ``None`` on any failure
    (missing asset for the platform, network error/timeout, checksum mismatch,
    extraction failure, or write/permission error). Never raises.
    """
    slug = _runtime_slug()
    asset = AST_GREP["ast_grep_release_assets"].get(slug)
    if not asset:
        logger.error(
            "ast-grep {} has no release asset for platform slug {!r}",
            AST_GREP["ast_grep_pinned_version"],
            slug,
        )
        return None

    destination = runtime_dir() / _binary_name()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("Cannot create ast-grep runtime dir {}: {}", destination.parent, exc)
        return None

    try:
        archive = _download(asset["url"], AST_GREP["ast_grep_provision_timeout_s"])
    except Exception as exc:  # noqa: BLE001 - any download failure must degrade, not crash
        logger.error(
            "Failed to download ast-grep {}: {}",
            AST_GREP["ast_grep_pinned_version"],
            exc,
        )
        return None

    actual_sha = _sha256(archive)
    expected_sha = asset["sha256"]
    if actual_sha != expected_sha:
        logger.error(
            "ast-grep checksum mismatch (refusing to install): expected {}, got {}",
            expected_sha[:16],
            actual_sha[:16],
        )
        return None

    try:
        binary_bytes = _extract_binary(archive, sys.platform)
    except (FileNotFoundError, zipfile.BadZipFile, OSError) as exc:
        logger.error("Failed to extract ast-grep binary: {}", exc)
        return None

    try:
        _write_atomic(destination, binary_bytes)
    except OSError as exc:
        logger.error("Failed to write ast-grep binary to {}: {}", destination, exc)
        return None

    logger.info(
        "ast-grep {} provisioned to {}",
        AST_GREP["ast_grep_pinned_version"],
        destination,
    )
    _clear_cache_for_tests()
    return str(destination)
