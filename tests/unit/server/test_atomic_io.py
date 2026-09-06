"""TDD tests for audit 2.1.8 — shared ``atomic_write_text`` (server/utils/atomic_io.py).

Pins the atomic-write contract the channel-config / scan-cache / skills-state
writers shared: tempfile in the target's directory + ``os.replace``, parent
dirs created, temp file cleaned up on failure, and the exception propagating
to the caller (error policy stays at call sites).
"""

import os

import pytest

from server.utils.atomic_io import atomic_write_text

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class TestAtomicWriteText:
    def test_writes_text_utf8(self, tmp_path):
        target = tmp_path / "config.json"

        atomic_write_text(target, '{"a": "小兰"}', )

        assert target.read_text(encoding="utf-8") == '{"a": "小兰"}'

    def test_overwrites_existing_file(self, tmp_path):
        target = tmp_path / "state.json"
        target.write_text("old", encoding="utf-8")

        atomic_write_text(target, "new", fsync=True)

        assert target.read_text(encoding="utf-8") == "new"

    def test_creates_missing_parent_dirs(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "file.txt"

        atomic_write_text(target, "payload")

        assert target.read_text(encoding="utf-8") == "payload"

    def test_accepts_str_path(self, tmp_path):
        target = tmp_path / "as_str.txt"

        atomic_write_text(str(target), "x")

        assert target.read_text(encoding="utf-8") == "x"

    def test_failure_leaves_original_intact_and_no_temp_files(self, tmp_path, monkeypatch):
        target = tmp_path / "config.json"
        target.write_text("original", encoding="utf-8")

        def _boom(*args, **kwargs):
            raise OSError("replace failed")

        monkeypatch.setattr(os, "replace", _boom)

        with pytest.raises(OSError, match="replace failed"):
            atomic_write_text(target, "partial")

        assert target.read_text(encoding="utf-8") == "original"
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "config.json"]
        assert leftovers == []

    def test_mkdir_failure_propagates(self, tmp_path):
        # A file where a parent dir is needed → mkstemp/mkdir cannot succeed.
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")
        target = blocker / "child" / "f.txt"

        with pytest.raises(OSError):
            atomic_write_text(target, "x")
