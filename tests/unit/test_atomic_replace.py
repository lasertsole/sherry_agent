"""Unit tests for pub_func/atomic_replace.py.

Covers the atomic move helper, including symlink preservation.
Symlink tests are guarded by os.symlink availability (Windows may
require admin privileges or Developer Mode).
"""

import os
import pytest

from pub_func.atomic_replace import atomic_replace


def _symlink_supported() -> bool:
    return hasattr(os, "symlink")


class TestAtomicReplace:
    def test_plain_move_replaces_target(self, tmp_path):
        tmp_file = tmp_path / "tmp_upload"
        target = tmp_path / "target.txt"
        tmp_file.write_text("new content", encoding="utf-8")
        target.write_text("old content", encoding="utf-8")

        returned = atomic_replace(str(tmp_file), str(target))

        assert target.read_text(encoding="utf-8") == "new content"
        assert returned == str(target)
        # the temp file should no longer exist after the move
        assert not tmp_file.exists()

    def test_target_does_not_exist_creates_it(self, tmp_path):
        tmp_file = tmp_path / "tmp_upload"
        target = tmp_path / "brand_new.txt"
        tmp_file.write_text("fresh", encoding="utf-8")

        atomic_replace(str(tmp_file), str(target))

        assert target.read_text(encoding="utf-8") == "fresh"

    def test_accepts_path_objects(self, tmp_path):
        tmp_file = tmp_path / "tmp_upload"
        target = tmp_path / "target.txt"
        tmp_file.write_text("path objects", encoding="utf-8")

        atomic_replace(tmp_file, target)

        assert target.read_text(encoding="utf-8") == "path objects"

    @pytest.mark.skipif(
        not _symlink_supported(),
        reason="os.symlink not supported on this platform",
    )
    def test_symlink_preserved_and_real_file_written(self, tmp_path):
        real = tmp_path / "real_config.yaml"
        real.write_text("original", encoding="utf-8")
        link = tmp_path / "config.yaml"
        # creating a symlink may fail on Windows without Developer Mode
        try:
            os.symlink(str(real), str(link))
        except (OSError, NotImplementedError):
            pytest.skip("cannot create symlink on this platform")

        tmp_file = tmp_path / "tmp_upload"
        tmp_file.write_text("updated", encoding="utf-8")

        returned = atomic_replace(str(tmp_file), str(link))

        # the symlink itself must survive
        assert os.path.islink(str(link)) is True
        # and the real target received the new content
        assert real.read_text(encoding="utf-8") == "updated"
        # returned path points at the real file
        assert returned == str(real)
