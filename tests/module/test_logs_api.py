"""Module tests for server/trigger/http/logs.py — log list, path sandbox, and tail reading.

These tests exercise the pure helper functions directly (no Robyn server is started):
    - _list_log_files: enumeration & metadata of unpacked ``.log`` files
    - _resolve_log_path: path sandboxing (must reject escapes, non-.log, missing files)
    - _tail_lines: efficient trailing-line reads (empty, small, large, boundary)
"""

import os
import pytest
from pathlib import Path
from unittest.mock import patch

from server.trigger.http import logs


@pytest.fixture
def log_dir(tmp_path: Path):
    """Create a temp log dir with a couple of sample ``.log`` files and a ``.zip``."""
    d = tmp_path / "output"
    d.mkdir(parents=True)
    return d


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# _list_log_files
# ---------------------------------------------------------------------------


class TestListLogFiles:
    def test_empty_dir(self, log_dir: Path):
        assert logs._list_log_files(log_dir) == []

    def test_missing_dir(self, tmp_path: Path):
        assert logs._list_log_files(tmp_path / "nope") == []

    def test_scans_type_subdirs_filters_zip_and_dirs(self, log_dir: Path):
        # Logs live in per-type sub-directories (``info/`` / ``all/`` / ``error/``).
        _write(log_dir / "info" / "info_2024-01-01_1234.log", "line1\nline2\n")
        _write(log_dir / "all" / "all_2024-01-01_1234.log", "trace\n")
        _write(log_dir / "error" / "error_2024-01-02_5678.log", "boom\n")
        # Only unpacked ``.log`` files are considered.
        log_dir.joinpath("info", "info_2024-01-03_9999.log.zip").write_text(
            "dummy", encoding="utf-8"
        )
        # Ignore non-log directories and non-log files entirely.
        (log_dir / "other").mkdir()
        (log_dir / "other" / "info_2024-01-04_1111.log").write_text("x", encoding="utf-8")
        (log_dir / "readme.txt").write_text("x", encoding="utf-8")

        files = logs._list_log_files(log_dir)
        names = {f["name"] for f in files}
        assert "info_2024-01-01_1234.log" in names
        assert "all_2024-01-01_1234.log" in names
        assert "error_2024-01-02_5678.log" in names
        assert "info_2024-01-03_9999.log.zip" not in names
        assert not any(f["name"].startswith("info_2024-01-04") for f in files)

    def test_metadata_fields(self, log_dir: Path):
        p = log_dir / "info" / "info_x.log"
        _write(p, "abc\n")
        files = logs._list_log_files(log_dir)
        assert len(files) == 1
        f = files[0]
        assert f["name"] == "info_x.log"
        assert f["path"] == str(p.resolve())
        # Size must match the exact bytes written, regardless of OS newline handling.
        assert f["size"] == len(p.read_bytes())
        assert f["is_error"] is False
        assert "T" in f["modified"]  # ISO-8601 contains date/time separator

    def test_is_error_flag(self, log_dir: Path):
        # ``is_error`` is driven both by the ``error`` sub-directory and the
        # ``error`` filename prefix.
        _write(log_dir / "error" / "error_2024-01-01_1.log", "e\n")
        _write(log_dir / "info" / "info_2024-01-01_1.log", "i\n")
        # A file inside the ``error`` dir with a non-``error`` name is still an error log.
        _write(log_dir / "error" / "anomalous_2024-01-02_2.log", "e2\n")
        # Full logs in the ``all`` dir are never flagged as error, even when
        # they carry an ``error``-suffixed name.
        _write(log_dir / "all" / "all_2024-01-03_3.log", "trace\n")
        _write(log_dir / "all" / "error_2024-01-03_3.log", "e3\n")
        files = logs._list_log_files(log_dir)
        by_name = {f["name"]: f["is_error"] for f in files}
        assert by_name["error_2024-01-01_1.log"] is True
        assert by_name["info_2024-01-01_1.log"] is False
        assert by_name["anomalous_2024-01-02_2.log"] is True
        assert by_name["all_2024-01-03_3.log"] is False
        assert by_name["error_2024-01-03_3.log"] is True

    def test_sorted_newest_first(self, log_dir: Path):
        # Root-level and sub-directory files are merged and sorted together.
        old = log_dir / "info" / "a.log"
        new = log_dir / "b.log"
        _write(old, "old\n")
        _write(new, "new\n")
        # Give "new" a later mtime to guarantee ordering.
        t_old = old.stat().st_mtime
        os.utime(new, (t_old + 10, t_old + 10))
        files = logs._list_log_files(log_dir)
        assert files[0]["name"] == "b.log"
        assert files[1]["name"] == "a.log"

    def test_is_current_matches_running_pid(self, log_dir: Path):
        # A file whose embedded PID equals the running process must be flagged
        # as current; any other PID must not. This holds for every log kind
        # (info / all / error).
        pid = os.getpid()
        _write(log_dir / "info" / f"info_2024-01-01_{pid}.log", "cur\n")
        _write(log_dir / "all" / f"all_2024-01-01_{pid}.log", "cur-all\n")
        _write(log_dir / "error" / f"error_2024-01-01_{pid}.log", "cur-err\n")
        _write(log_dir / "all" / "all_2024-01-01_999999.log", "old-all\n")
        _write(log_dir / "info" / "info_2024-01-01_999999.log", "old\n")
        # Non-PID names never count as current.
        _write(log_dir / "info" / "info_arbitrary.log", "x\n")

        files = logs._list_log_files(log_dir)
        by_name = {f["name"]: f["is_current"] for f in files}
        assert by_name[f"info_2024-01-01_{pid}.log"] is True
        assert by_name[f"all_2024-01-01_{pid}.log"] is True
        assert by_name[f"error_2024-01-01_{pid}.log"] is True
        assert by_name["all_2024-01-01_999999.log"] is False
        assert by_name["info_2024-01-01_999999.log"] is False
        assert by_name["info_arbitrary.log"] is False


# ---------------------------------------------------------------------------
# _resolve_log_path
# ---------------------------------------------------------------------------


class TestResolveLogPath:
    @patch.object(logs, "LOG_DIR", lambda: None)  # placeholder; replaced below
    def _ctx(self, log_dir: Path):
        return patch.object(logs, "LOG_DIR", log_dir.resolve())

    def test_accepts_inner_file(self, log_dir: Path):
        p = log_dir / "info_2024-01-01_1.log"
        _write(p, "x\n")
        with self._ctx(log_dir):
            assert logs._resolve_log_path(str(p)) == p.resolve()

    def test_accepts_url_encoded_inner_file(self, log_dir: Path):
        # The path travels as a URL query param, so backslashes/colons arrive
        # percent-encoded (e.g. `C:%5C...`). It must be unquoted before use.
        import urllib.parse

        p = log_dir / "info_2024-01-01_2.log"
        _write(p, "x\n")
        encoded = urllib.parse.quote(str(p))
        with self._ctx(log_dir):
            assert logs._resolve_log_path(encoded) == p.resolve()
            # Also tolerate mixed literal + encoded segments.
            head, _, tail = str(p).rpartition("info_2024-01-01_2.log")
            mixed = head + "info_2024-01-01_2.log"
            assert logs._resolve_log_path(mixed) == p.resolve()

    def test_rejects_empty(self, log_dir: Path):
        with self._ctx(log_dir):
            assert logs._resolve_log_path("") is None
            assert logs._resolve_log_path(None) is None

    def test_rejects_missing_file(self, log_dir: Path):
        with self._ctx(log_dir):
            assert logs._resolve_log_path(str(log_dir / "missing.log")) is None

    def test_rejects_non_log_suffix(self, log_dir: Path):
        p = log_dir / "readme.txt"
        _write(p, "x")
        with self._ctx(log_dir):
            assert logs._resolve_log_path(str(p)) is None

    def test_rejects_path_escape(self, log_dir: Path, tmp_path: Path):
        # A real .log file OUTSIDE the sandboxed log dir must be rejected.
        outside = tmp_path / "outside" / "evil.log"
        _write(outside, "evil\n")
        with self._ctx(log_dir):
            assert logs._resolve_log_path(str(outside)) is None

    def test_rejects_dotdot_escape(self, log_dir: Path, tmp_path: Path):
        _write(tmp_path / "leak.log", "secret\n")
        roam = log_dir / "roaming" / ".." / ".." / "leak.log"
        with self._ctx(log_dir):
            assert logs._resolve_log_path(str(roam)) is None

    def test_rejects_directory(self, log_dir: Path):
        with self._ctx(log_dir):
            assert logs._resolve_log_path(str(log_dir)) is None


# ---------------------------------------------------------------------------
# _tail_lines
# ---------------------------------------------------------------------------


class TestTailLines:
    def test_empty_file(self, tmp_path: Path):
        p = tmp_path / "e.log"
        p.write_text("", encoding="utf-8")
        assert logs._tail_lines(p, 500) == ""

    def test_small_file_respects_lines(self, tmp_path: Path):
        p = tmp_path / "s.log"
        p.write_text("a\nb\nc\n", encoding="utf-8")
        # Even small files must honor the requested `lines` limit.
        assert logs._tail_lines(p, 3) == "a\nb\nc"
        assert logs._tail_lines(p, 2) == "b\nc"
        assert logs._tail_lines(p, 1) == "c"

    def test_large_file_tails(self, tmp_path: Path):
        p = tmp_path / "l.log"
        # 2000 lines with varying content to defeat any accidental whole-file read path.
        p.write_text("".join(f"line-{i:04d}\n" for i in range(2000)), encoding="utf-8")
        tail = logs._tail_lines(p, 50)
        lines = tail.split("\n")
        assert lines[0] == "line-1950"
        assert lines[-1] == "line-1999"
        assert len(lines) == 50

    def test_tail_less_than_total(self, tmp_path: Path):
        p = tmp_path / "m.log"
        p.write_text("".join(f"{i:05d}\n" for i in range(100)), encoding="utf-8")
        tail = logs._tail_lines(p, 10)
        assert tail.split("\n")[0] == "00090"

    def test_tail_more_than_total_returns_all(self, tmp_path: Path):
        p = tmp_path / "many.log"
        p.write_text("a\nb\nc\n", encoding="utf-8")
        assert logs._tail_lines(p, 1000) == "a\nb\nc"

    def test_no_trailing_newline_last_line(self, tmp_path: Path):
        p = tmp_path / "nt.log"
        p.write_text("x\ny\nz", encoding="utf-8")
        assert logs._tail_lines(p, 100) == "x\ny\nz"

    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert logs._tail_lines(tmp_path / "gone.log", 100) == ""


# ---------------------------------------------------------------------------
# WebSocket sink + serializer (pure helpers)
# ---------------------------------------------------------------------------


class TestLogSinkSerialization:
    def test_serialize_record_shape(self):
        # `_serialize_record` lives in the WebSocket module, so import it there.
        from server.trigger.ws import logs as ws_logs

        record_dict = {
            "time": "2024-01-01T10:00:00.000000",
            "level": type("L", (), {"name": "INFO"})(),
            "name": "__main__",
            "function": "main",
            "line": 42,
            "message": "hello world",
        }
        frame = ws_logs._serialize_record(record_dict)
        import json

        obj = json.loads(frame)
        assert obj["event"] == "log"
        assert obj["data"]["level"] == "INFO"
        assert obj["data"]["message"] == "hello world"
        assert obj["data"]["line"] == 42
