"""End-to-end: what lands in the real log files after a secret is logged (B5).

This runs the production logging stack — ``logs/logger.py::init_logger``, which
removes loguru's defaults and installs the console plus three rotating file sinks
with ``enqueue=True`` — writes secrets through the plain ``logger`` the rest of
the codebase uses, and then reads the FILES back.

It runs in a subprocess on purpose: ``init_logger`` owns the global logger for
the life of the process (it calls ``logger.remove()`` and starts background
writers), which would leak into every other test in the file.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

_PROBE = """
import pathlib, tempfile
from loguru import logger
from logs.logger import init_logger

log_dir = pathlib.Path(tempfile.mkdtemp())
init_logger(log_dir=log_dir, timeout_days=7)

secrets = {
    "openai": "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "bearer": "abcdefghijklmnop.qrstuvwx",
    "password": "hunter2hunter2",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
}
logger.info("provider key {}", secrets["openai"])
logger.warning("Authorization: Bearer {}", secrets["bearer"])
logger.error("db password: {}", secrets["password"])
logger.debug("session {}", secrets["jwt"])
logger.info("ordinary line: the token budget was refreshed")
logger.complete()  # flush the enqueue=True sinks

text = "\\n".join(p.read_text(encoding="utf-8") for p in log_dir.rglob("*.log"))
print("LEAKED:" + ",".join(name for name, value in secrets.items() if value in text))
print("SENTINEL:" + str("\\u00abredacted" in text))
print("ORDINARY:" + str("the token budget was refreshed" in text))
print("FILES:" + str(len(list(log_dir.rglob("*.log")))))
"""


def _run_probe() -> dict[str, str]:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    lines = {}
    for line in result.stdout.splitlines():
        for prefix in ("LEAKED:", "SENTINEL:", "ORDINARY:", "FILES:"):
            if line.startswith(prefix):
                lines[prefix[:-1]] = line[len(prefix) :]
    assert set(lines) == {"LEAKED", "SENTINEL", "ORDINARY", "FILES"}, result.stdout
    return lines


def test_no_secret_reaches_the_log_files_and_ordinary_text_survives():
    out = _run_probe()

    assert out["LEAKED"] == "", f"secrets written to the log files: {out['LEAKED']}"
    assert out["SENTINEL"] == "True", "no redaction sentinel: the patcher is not installed"
    assert out["ORDINARY"] == "True", "redaction corrupted ordinary log text"
    assert int(out["FILES"]) >= 3, f"expected the info/all/error sinks, got {out['FILES']}"
