"""Log retention: the age sweep and the total-size cap (design concept #23).

The per-PID log files accumulate for as long as the backend runs, so the
retention rules live OUTSIDE the sink configuration: a boot-time sweep, a
total-size cap that deletes oldest-first, and a background sweeper so a
long-running process keeps pruning. These tests drive the two sweep functions
directly against a throwaway tree — ``init_logger`` reconfigures the GLOBAL
loguru logger and is never called in-process here.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from config.features import LOGS
from logs.logger import _clean_expired_logs, _enforce_total_size

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


def _make_tree(base: Path, *names: str, age_s: float = 0.0, size: int = 10) -> list[Path]:
    """Create ``<base>/<name>`` files with the given age and size."""
    paths: list[Path] = []
    for name in names:
        p = base / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        stamp = time.time() - age_s
        os.utime(p, (stamp, stamp))
        paths.append(p)
    return paths


@pytest.fixture()
def log_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "logs"
    tree.mkdir()
    return tree


def test_the_age_sweep_deletes_old_files_and_keeps_fresh_ones(log_tree: Path) -> None:
    old = _make_tree(log_tree, "json/json_old_1.log", "error/error_old.zip", age_s=8 * 86400)
    fresh = _make_tree(log_tree, "json/json_new_2.log", age_s=60)

    _clean_expired_logs(log_tree, timeout_days=7)

    assert not old[0].exists() and not old[1].exists()
    assert fresh[0].exists()


def test_a_zero_retention_disables_the_age_rule(log_tree: Path) -> None:
    ancient = _make_tree(log_tree, "json/json_ancient.log", age_s=400 * 86400)

    _clean_expired_logs(log_tree, timeout_days=0)

    assert ancient[0].exists()


def test_the_size_cap_deletes_oldest_files_first(log_tree: Path) -> None:
    # Three 1 MiB files with distinct ages; a 2 MiB cap must drop the oldest.
    for name, age in (("a_old.log", 100_000), ("b_mid.log", 50_000), ("c_new.log", 10)):
        _make_tree(log_tree, f"json/{name}", age_s=age, size=1024 * 1024)

    removed = _enforce_total_size(log_tree, max_total_mb=2)

    assert removed == 1
    assert not (log_tree / "json" / "a_old.log").exists()
    assert (log_tree / "json" / "b_mid.log").exists()
    assert (log_tree / "json" / "c_new.log").exists()


def test_the_size_cap_never_touches_actively_written_files(log_tree: Path) -> None:
    # A single fresh file over the cap: the quiescence guard skips it.
    fresh = _make_tree(log_tree, "json/json_now.log", age_s=1, size=3 * 1024 * 1024)

    removed = _enforce_total_size(log_tree, max_total_mb=2)

    assert removed == 0
    assert fresh[0].exists()


def test_a_zero_cap_disables_the_size_rule(log_tree: Path) -> None:
    big = _make_tree(log_tree, "json/json_big.log", age_s=10_000, size=5 * 1024 * 1024)

    assert _enforce_total_size(log_tree, max_total_mb=0) == 0
    assert big[0].exists()


def test_the_config_keys_carry_the_shipped_defaults() -> None:
    assert LOGS["retention_days"] == 7
    assert LOGS["max_total_mb"] == 512
    assert LOGS["sweep_interval_s"] == 86400
