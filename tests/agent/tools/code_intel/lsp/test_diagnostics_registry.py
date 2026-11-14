"""The process-level diagnostics registry: delivery, change detection, scoping.

Passive injection depends on these rules: errors are delivered once per session
per diagnostic generation, an identical republish does not re-inject, a changed
publish does, and only files under the session's root are eligible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.code_intel.lsp import diagnostics
from agent.tools.code_intel.lsp.protocol import path_to_uri

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_ERROR = {"message": "boom", "severity": 1, "source": "fake", "code": "E1"}
_WARNING = {"message": "meh", "severity": 2}


@pytest.fixture(autouse=True)
def _clean_registry():
    diagnostics.reset_for_tests()
    yield
    diagnostics.reset_for_tests()


def _publish(path: Path, items: list[dict], client_key: str = "python:/root") -> None:
    diagnostics.publish(client_key, path_to_uri(str(path)), items)


def test_errors_are_delivered_once_per_session(tmp_path: Path):
    target = tmp_path / "a.py"
    target.write_text("x\n", encoding="utf-8")
    _publish(target, [_ERROR, _WARNING])

    entries = diagnostics.undelivered_errors("s1", tmp_path, max_files=10, max_errors=10)
    assert entries == [(str(target.resolve()), [_ERROR])]

    diagnostics.mark_delivered("s1", [entries[0][0]])
    assert diagnostics.undelivered_errors("s1", tmp_path, max_files=10, max_errors=10) == []

    # Another session sharing the root still gets it.
    assert len(diagnostics.undelivered_errors("s2", tmp_path, max_files=10, max_errors=10)) == 1


def test_identical_republish_does_not_reinject(tmp_path: Path):
    target = tmp_path / "a.py"
    target.write_text("x\n", encoding="utf-8")
    _publish(target, [_ERROR])
    diagnostics.mark_delivered("s1", [str(target.resolve())])

    _publish(target, [dict(_ERROR)])

    assert diagnostics.undelivered_errors("s1", tmp_path, max_files=10, max_errors=10) == []


def test_a_changed_publish_reopens_delivery(tmp_path: Path):
    target = tmp_path / "a.py"
    target.write_text("x\n", encoding="utf-8")
    _publish(target, [_ERROR])
    diagnostics.mark_delivered("s1", [str(target.resolve())])

    _publish(target, [dict(_ERROR, message="a different error")])

    entries = diagnostics.undelivered_errors("s1", tmp_path, max_files=10, max_errors=10)
    assert len(entries) == 1
    assert entries[0][1][0]["message"] == "a different error"


def test_files_outside_the_root_and_missing_files_are_skipped(tmp_path: Path):
    outside = tmp_path.parent / "outside.py"
    outside.write_text("x\n", encoding="utf-8")
    _publish(outside, [_ERROR])
    missing = tmp_path / "gone.py"
    _publish(missing, [_ERROR])

    assert diagnostics.undelivered_errors("s1", tmp_path, max_files=10, max_errors=10) == []


def test_clear_client_drops_its_files(tmp_path: Path):
    target = tmp_path / "a.py"
    target.write_text("x\n", encoding="utf-8")
    _publish(target, [_ERROR], client_key="python:/root")

    diagnostics.clear_client("python:/root")

    assert diagnostics.undelivered_errors("s1", tmp_path, max_files=10, max_errors=10) == []


def test_caps_keep_the_newest_files_and_errors(tmp_path: Path):
    for index in range(3):
        target = tmp_path / f"f{index}.py"
        target.write_text("x\n", encoding="utf-8")
        _publish(target, [dict(_ERROR, message=f"m{index}"), dict(_ERROR, message=f"n{index}")])

    entries = diagnostics.undelivered_errors("s1", tmp_path, max_files=2, max_errors=3)

    assert len(entries) == 2
    assert sum(len(items) for _path, items in entries) == 3


def test_empty_publish_clears_the_error(tmp_path: Path):
    target = tmp_path / "a.py"
    target.write_text("x\n", encoding="utf-8")
    _publish(target, [_ERROR])
    _publish(target, [])

    assert diagnostics.undelivered_errors("s1", tmp_path, max_files=10, max_errors=10) == []
    assert diagnostics.snapshot() == [
        {
            "path": str(target.resolve()),
            "client_key": "python:/root",
            "errors": 0,
            "total": 0,
            "delivered_sessions": [],
        }
    ]
