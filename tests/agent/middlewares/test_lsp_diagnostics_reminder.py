"""The passive LSP-diagnostics reminder: errors once per generation, per session."""

from __future__ import annotations

from pathlib import Path

import pytest

import agent.tools.pub_base as pub_base
from agent.middlewares.reminder_catalog import sources
from agent.middlewares.reminder_catalog.core import registered_sources
from agent.tools.code_intel.lsp import diagnostics
from agent.tools.code_intel.lsp.protocol import path_to_uri

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_ERROR = {
    "message": "undefined name 'foo'",
    "severity": 1,
    "source": "Pyright",
    "code": "reportUndefinedVariable",
}
_WARNING = {"message": "unused import", "severity": 2}


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    diagnostics.reset_for_tests()
    monkeypatch.setattr(pub_base, "session_workspace_root", lambda _sid: tmp_path)
    yield
    diagnostics.reset_for_tests()


def _file(tmp_path: Path) -> Path:
    target = tmp_path / "app.py"
    target.write_text("foo\n", encoding="utf-8")
    return target


def test_the_source_is_registered_high_and_transient():
    sources.register_default_sources()
    source = registered_sources()["lsp_diagnostics"]

    assert source.priority == 84
    assert source.transient is True
    assert source.budget_chars == 3000


def test_errors_are_rendered_with_location_and_source(tmp_path: Path):
    target = _file(tmp_path)
    diagnostics.publish("python:/root", path_to_uri(str(target)), [_ERROR, _WARNING])

    text = sources._lsp_diagnostics_builder("s1", {})

    assert text is not None
    assert "LSP DIAGNOSTICS" in text
    assert f"{target.name}:1:1 undefined name 'foo'" in text
    assert "Pyright:reportUndefinedVariable" in text
    assert "unused import" not in text


def test_delivery_is_once_per_generation(tmp_path: Path):
    target = _file(tmp_path)
    diagnostics.publish("python:/root", path_to_uri(str(target)), [_ERROR])

    assert sources._lsp_diagnostics_builder("s1", {}) is not None
    assert sources._lsp_diagnostics_builder("s1", {}) is None

    diagnostics.publish(
        "python:/root", path_to_uri(str(target)), [dict(_ERROR, message="a new error")]
    )
    text = sources._lsp_diagnostics_builder("s1", {})
    assert text is not None and "a new error" in text


def test_another_session_still_sees_the_error(tmp_path: Path):
    target = _file(tmp_path)
    diagnostics.publish("python:/root", path_to_uri(str(target)), [_ERROR])

    assert sources._lsp_diagnostics_builder("s1", {}) is not None
    assert sources._lsp_diagnostics_builder("s2", {}) is not None
