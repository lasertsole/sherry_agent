"""The frozen backend entry's daemon dispatch.

A packaged backend cannot run ``python -c``, so the local model daemons re-execute
the same binary with ``--sherry-daemon <module>``; this pins the parser and that
the daemon branch never boots the server (which would initialise logging and
event-loop patching on the way to a model loader).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "packaging" / "backend_entry.py"


def _load_entry():
    spec = importlib.util.spec_from_file_location("sherry_backend_entry", ENTRY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parses_the_equals_and_space_forms():
    entry = _load_entry()

    assert (
        entry.daemon_module(["--sherry-daemon", "models.embed_model.text.daemon"])
        == "models.embed_model.text.daemon"
    )
    assert (
        entry.daemon_module(["--sherry-daemon=models.reranker_model.daemon"])
        == "models.reranker_model.daemon"
    )


def test_absent_or_empty_flags_answer_none():
    entry = _load_entry()

    assert entry.daemon_module([]) is None
    assert entry.daemon_module(["--sherry-home", "/data"]) is None
    assert entry.daemon_module(["--sherry-daemon"]) is None
    assert entry.daemon_module(["--sherry-daemon="]) is None
    assert entry.daemon_module(["--sherry-daemon", "   "]) is None


def test_the_daemon_branch_runs_main_and_returns(monkeypatch):
    entry = _load_entry()
    called: list[int] = []
    fake = types.ModuleType("fake.daemon")
    fake.main = lambda: called.append(1)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake.daemon", fake)
    monkeypatch.setattr(entry, "daemon_module", lambda: "fake.daemon")

    entry.main()

    assert called == [1]
