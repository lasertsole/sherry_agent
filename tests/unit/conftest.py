"""Shared fixtures for the flat unit tests in tests/unit/.

The SkillSpector scan cache (server/service/skill_scanner.py) persists
verdicts in a content-addressed JSON file under ``src/data/``. Unit tests
that exercise ``scan_skill`` with faked backends (test_skill_scanner.py)
must never read or write that real file: tmp-path skill content is
byte-identical across pytest runs, so entries persisted on one run would
turn later runs' cold scans into warm cache hits and break
``assert_called_once`` style assertions. Redirect the cache into per-test
tmp storage for every unit test.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

logger = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def _isolated_skill_scan_cache(tmp_path):
    """Point the SkillSpector verdict cache at per-test tmp storage and freeze
    the scanner-version fingerprint.

    Freezing ``_scanner_version_fingerprint`` keeps unit tests fully hermetic
    (no real ``skillspector --version`` subprocess), makes cache keys
    deterministic, and prevents the probe from consuming patched
    ``subprocess.run`` calls that test_skill_scanner.py counts with
    ``assert_called_once``.
    """
    try:
        import server.service.skill_scanner  # noqa: F401
    except Exception:
        # PART2 §12: a broken skill_scanner import (e.g. langgraph
        # ExecutionInfo environment issues) must not crash every unit
        # test at fixture setup; degrade to no-patching so unrelated
        # tests keep running.
        logger.warning(
            "server.service.skill_scanner import failed; "
            "SkillSpector scan-cache isolation patches skipped",
            exc_info=True,
        )
        yield
        return
    with (
        patch(
            "server.service.skill_scanner._CACHE_PATH",
            tmp_path / "skills_scan_cache.json",
        ),
        patch(
            "server.service.skill_scanner._scanner_version_fingerprint",
            return_value="SkillSpector v-unit-stable",
        ),
    ):
        yield
