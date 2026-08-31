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

from unittest.mock import patch

import pytest


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
