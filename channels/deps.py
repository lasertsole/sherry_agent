"""Shared channel-plugin dependency installation (audit 3.1.4).

``channels/registry.py::_ensure_deps`` and
``plugins/channels/qq/core.py::_install_deps`` previously duplicated the same
install flow: prefer ``uv pip install -q -r`` (consistent with the project
toolchain), fall back to ``python -m pip``, 120 s subprocess timeout,
``importlib.invalidate_caches()`` on success so a just-installed package is
importable. The subprocess mechanics live here; each caller keeps its own
pre-checks, log wording, and failure policy (registry: fail the load; qq:
cooldown bookkeeping).
"""

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

_DEP_INSTALL_TIMEOUT_SECONDS = 120


def install_requirements(
    req_file: Path, *, timeout: int = _DEP_INSTALL_TIMEOUT_SECONDS
) -> tuple[bool, bool, str]:
    """Install *req_file* into the running environment (idempotent).

    Already-satisfied packages are skipped by pip/uv.

    Returns:
        ``(ok, timed_out, stderr_summary)`` — ``stderr_summary`` is the
        stripped combined stderr/stdout of a failed run (empty otherwise).
        On success the import-system caches are invalidated.
    """
    if shutil.which("uv"):
        cmd = ["uv", "pip", "install", "-q", "-r", str(req_file)]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "-q", "-r", str(req_file)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, True, ""

    if result.returncode != 0:
        # Use string formatting (not a {} placeholder) so Windows-GBK terminals
        # never choke decoding arbitrary pip stderr bytes inside loguru.
        return False, False, (result.stderr or result.stdout or "").strip()

    importlib.invalidate_caches()
    return True, False, ""
