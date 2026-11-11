"""Edit sync + debounced auto-diagnostics (fail-open, evidence-row contract)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import agent.tools.pub_base.lsp_sync as lsp_sync
from agent.tools.pub_base.evidence_ledger import EvidenceLedger

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _FakeClient:
    def __init__(self, published: list | None = None) -> None:
        self.synced: list[tuple[str, str]] = []
        self.published = published
        self.waits: list[tuple[str, int, float]] = []

    def sync_document(self, file_path: str, content: str) -> bool:
        self.synced.append((file_path, content))
        return True

    def cached_diagnostics(self, uri: str):
        return None

    def diagnostics_generation(self, uri: str) -> int:
        return 0

    def diagnostics_pending(self, uri: str) -> bool:
        return True

    def wait_diagnostics_since(self, uri: str, generation: int, timeout_s: float):
        self.waits.append((uri, generation, timeout_s))
        return self.published


@pytest.fixture()
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "ledger.db"
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(path))
    monkeypatch.setitem(
        __import__("config.features", fromlist=["LSP"]).LSP, "lsp_auto_diagnostics_enabled", True
    )
    return path


def _file(tmp_path: Path) -> Path:
    target = tmp_path / "app.py"
    target.write_text("foo\n", encoding="utf-8")
    return target


def _wire(monkeypatch: pytest.MonkeyPatch, client: _FakeClient | None) -> None:
    monkeypatch.setattr(lsp_sync, "_client_for", lambda _path: ("python", client))


def test_sync_edited_file_pushes_and_schedules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client = _FakeClient()
    scheduled: list[tuple[str, str]] = []
    _wire(monkeypatch, client)
    monkeypatch.setattr(
        lsp_sync, "schedule_auto_diagnostics", lambda path, sid: scheduled.append((path, sid))
    )
    target = _file(tmp_path)

    assert lsp_sync.sync_edited_file(str(target), "bar\n", "sess") is True
    assert client.synced == [(str(target), "bar\n")]
    assert scheduled == [(str(target), "sess")]


def test_sync_without_a_client_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _wire(monkeypatch, None)
    target = _file(tmp_path)

    assert lsp_sync.sync_edited_file(str(target), "bar\n", "sess") is False


def test_sync_is_fail_open_on_a_client_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class _Boom(_FakeClient):
        def sync_document(self, file_path: str, content: str) -> None:
            raise RuntimeError("pipe closed")

    _wire(monkeypatch, _Boom())
    target = _file(tmp_path)

    assert lsp_sync.sync_edited_file(str(target), "bar\n", "sess") is False


def test_sync_edited_path_reads_only_with_a_live_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _FakeClient()
    _wire(monkeypatch, client)
    target = _file(tmp_path)

    assert lsp_sync.sync_edited_path(str(target), "") is True
    assert client.synced == [(str(target), "foo\n")]


def test_run_once_writes_a_failed_row(tmp_path: Path, ledger: Path, monkeypatch):
    client = _FakeClient(
        published=[
            {
                "message": "undefined name",
                "severity": 1,
                "range": {"start": {"line": 2, "character": 4}, "end": {"line": 2, "character": 8}},
            }
        ]
    )
    _wire(monkeypatch, client)
    target = _file(tmp_path)

    lsp_sync._run_once(str(target), "sess")

    rows = EvidenceLedger.read_for_session("sess")
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "lsp_diagnostics"
    assert row["status"] == "failed"
    assert row["auto"] is True
    assert row["command"].startswith("lsp_diagnostics ")
    assert "undefined name" in row["output_snippet"]


def test_run_once_writes_a_passed_row_with_no_errors(tmp_path: Path, ledger: Path, monkeypatch):
    _wire(monkeypatch, _FakeClient(published=[]))
    target = _file(tmp_path)

    lsp_sync._run_once(str(target), "sess")

    rows = EvidenceLedger.read_for_session("sess")
    assert rows[0]["status"] == "passed"
    assert rows[0]["exit_code"] == 0


def test_run_once_writes_nothing_without_a_publish(tmp_path: Path, ledger: Path, monkeypatch):
    _wire(monkeypatch, _FakeClient(published=None))
    target = _file(tmp_path)

    lsp_sync._run_once(str(target), "sess")

    assert EvidenceLedger.read_for_session("sess") == []


def test_run_once_is_fail_open(tmp_path: Path, ledger: Path, monkeypatch):
    class _Boom(_FakeClient):
        def sync_document(self, file_path: str, content: str) -> None:
            raise RuntimeError("boom")

    _wire(monkeypatch, _Boom())
    target = _file(tmp_path)

    lsp_sync._run_once(str(target), "sess")  # must not raise

    assert EvidenceLedger.read_for_session("sess") == []


def test_schedule_is_disabled_by_the_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from config.features import LSP

    monkeypatch.setitem(LSP, "lsp_auto_diagnostics_enabled", False)
    _wire(monkeypatch, _FakeClient())
    scheduled: list[Any] = []
    monkeypatch.setattr(lsp_sync, "_submit", lambda *a: scheduled.append(a))

    lsp_sync.schedule_auto_diagnostics(str(_file(tmp_path)), "sess")

    assert scheduled == []
