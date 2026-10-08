"""Unit tests for the process-level LSP server manager (resource bounds + reaping)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from agent.tools.code_intel.lsp.manager import LspServerManager

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _assert_reaped(pid: int | None) -> None:
    assert pid is not None
    if sys.platform == "win32":
        return
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


class TestLazyAndReuse:
    def test_lazy_start_and_reuse(
        self, tmp_path: Path, point_to_fake, fake_lsp_server: Path
    ) -> None:
        point_to_fake("python")
        manager = LspServerManager(max_servers=2, idle_shutdown_s=0)
        cwd = tmp_path / "a"
        cwd.mkdir()
        try:
            assert manager.active_count() == 0
            client, error = manager.acquire("python", str(cwd), str(fake_lsp_server))
            assert error == ""
            assert client is not None and client.alive
            assert manager.active_count() == 1
            same, _ = manager.acquire("python", str(cwd), str(fake_lsp_server))
            assert same is client  # reused, not restarted
            assert manager.active_count() == 1
        finally:
            manager._clear_for_tests()

    def test_unknown_language_returns_error(self, tmp_path: Path, point_to_fake) -> None:
        point_to_fake("python")
        manager = LspServerManager(max_servers=2, idle_shutdown_s=0)
        client, error = manager.acquire("haskell", str(tmp_path), "/nonexistent")
        assert client is None
        assert "no LSP server" in error


class TestConcurrencyCap:
    def test_lru_eviction_at_limit(
        self, tmp_path: Path, point_to_fake, fake_lsp_server: Path
    ) -> None:
        point_to_fake("python")
        manager = LspServerManager(max_servers=1, idle_shutdown_s=0)
        cwd_a = tmp_path / "a"
        cwd_b = tmp_path / "b"
        cwd_a.mkdir()
        cwd_b.mkdir()
        try:
            first, _ = manager.acquire("python", str(cwd_a), str(fake_lsp_server))
            assert first is not None
            first_pid = first.pid
            second, _ = manager.acquire("python", str(cwd_b), str(fake_lsp_server))
            assert second is not None and second.alive
            assert not first.alive  # evicted to respect the cap
            assert manager.active_count() == 1
            _assert_reaped(first_pid)
        finally:
            manager._clear_for_tests()


class TestIdleShutdown:
    def test_sweep_reaps_idle_server(
        self, tmp_path: Path, point_to_fake, fake_lsp_server: Path
    ) -> None:
        point_to_fake("python")
        manager = LspServerManager(max_servers=2, idle_shutdown_s=0.05)
        cwd = tmp_path / "a"
        cwd.mkdir()
        try:
            client, _ = manager.acquire("python", str(cwd), str(fake_lsp_server))
            assert client is not None
            pid = client.pid
            client.last_used = time.time() - 100
            reaped = manager.sweep_idle()
            assert reaped
            assert manager.active_count() == 0
            assert not client.alive
            _assert_reaped(pid)
        finally:
            manager._clear_for_tests()

    def test_sweep_disabled_when_zero(
        self, tmp_path: Path, point_to_fake, fake_lsp_server: Path
    ) -> None:
        point_to_fake("python")
        manager = LspServerManager(max_servers=2, idle_shutdown_s=0)
        cwd = tmp_path / "a"
        cwd.mkdir()
        try:
            client, _ = manager.acquire("python", str(cwd), str(fake_lsp_server))
            assert client is not None
            client.last_used = time.time() - 100
            assert manager.sweep_idle() == []
            assert client.alive
        finally:
            manager._clear_for_tests()


class TestShutdownAll:
    def test_shutdown_all_reaps_every_server(
        self, tmp_path: Path, point_to_fake, fake_lsp_server: Path
    ) -> None:
        point_to_fake("python")
        manager = LspServerManager(max_servers=4, idle_shutdown_s=0)
        pids = []
        for name in ("a", "b"):
            cwd = tmp_path / name
            cwd.mkdir()
            client, _ = manager.acquire("python", str(cwd), str(fake_lsp_server))
            assert client is not None
            pids.append(client.pid)
        assert manager.active_count() == 2
        manager.shutdown_all()
        assert manager.active_count() == 0
        for pid in pids:
            _assert_reaped(pid)
