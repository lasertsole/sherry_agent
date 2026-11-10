"""Content-addressed artifact store: dedupe, URI parsing, path safety."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent.tools.pub_base import artifact_store
from config.features import MEDIA_PIPELINE

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

_PNG = b"\x89PNG\r\n\x1a\n" + b"artifact-body"


def _artifacts(root):
    return sorted((root / "s1" / artifact_store.ARTIFACT_DIR_NAME).glob("*"))


class TestWriteArtifact:
    def test_write_returns_a_uri_and_stores_the_bytes(self, tmp_path):
        uri = artifact_store.write_artifact(
            "s1", _PNG, media_type="image", ext=".png", root=tmp_path
        )

        assert uri is not None
        assert uri.startswith("sherry-artifact://s1/image-")
        path = artifact_store.resolve_artifact(uri, root=tmp_path)
        assert path is not None
        assert path.read_bytes() == _PNG

    def test_identical_bytes_deduplicate_to_one_file(self, tmp_path):
        first = artifact_store.write_artifact(
            "s1", _PNG, media_type="image", ext=".png", root=tmp_path
        )
        path = artifact_store.resolve_artifact(first, root=tmp_path)
        assert path is not None
        mtime = path.stat().st_mtime_ns

        second = artifact_store.write_artifact(
            "s1", _PNG, media_type="image", ext=".png", root=tmp_path
        )

        assert second == first
        assert len(_artifacts(tmp_path)) == 1
        assert path.stat().st_mtime_ns == mtime

    def test_different_bytes_get_different_uris(self, tmp_path):
        first = artifact_store.write_artifact(
            "s1", _PNG, media_type="image", ext=".png", root=tmp_path
        )
        second = artifact_store.write_artifact(
            "s1", _PNG + b"x", media_type="image", ext=".png", root=tmp_path
        )

        assert first != second
        assert len(_artifacts(tmp_path)) == 2

    def test_concurrent_writes_of_the_same_bytes_deduplicate(self, tmp_path):
        def write(_):
            return artifact_store.write_artifact(
                "s1", _PNG, media_type="image", ext=".png", root=tmp_path
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            uris = list(pool.map(write, range(16)))

        assert len(set(uris)) == 1
        assert len(_artifacts(tmp_path)) == 1

    def test_store_can_be_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setitem(MEDIA_PIPELINE, "artifact_store_enabled", False)

        assert (
            artifact_store.write_artifact("s1", _PNG, media_type="image", ext=".png", root=tmp_path)
            is None
        )

    def test_unsafe_session_is_refused(self, tmp_path):
        assert (
            artifact_store.write_artifact(
                "../escape", _PNG, media_type="image", ext=".png", root=tmp_path
            )
            is None
        )

    def test_an_unsafe_extension_degrades_to_bin(self, tmp_path):
        uri = artifact_store.write_artifact(
            "s1", _PNG, media_type="image", ext="../../x", root=tmp_path
        )

        assert uri is not None and uri.endswith(".bin")

    def test_unknown_family_is_stored_as_binary(self, tmp_path):
        uri = artifact_store.write_artifact(
            "s1", _PNG, media_type="weird", ext=".dat", root=tmp_path
        )

        assert uri is not None and "/binary-" in uri


class TestResolveArtifact:
    def test_parse_round_trips(self):
        uri = artifact_store.artifact_uri("s1", "image-" + "a" * 16 + ".png")

        assert artifact_store.parse_artifact_uri(uri) == (
            "s1",
            "image-" + "a" * 16 + ".png",
        )

    @pytest.mark.parametrize(
        "uri",
        [
            "",
            "https://example.test/a.png",
            "sherry-artifact://s1",
            "sherry-artifact:///image-" + "a" * 16 + ".png",
            "sherry-artifact://s1/../escape.png",
            "sherry-artifact://s1/image-notahash.png",
            "sherry-artifact://s1/image-" + "a" * 16 + ".png/extra",
        ],
    )
    def test_invalid_uris_do_not_resolve(self, uri):
        assert artifact_store.parse_artifact_uri(uri) is None
        assert artifact_store.resolve_artifact(uri, root=Path("/tmp")) is None

    def test_missing_file_does_not_resolve(self, tmp_path):
        uri = artifact_store.artifact_uri("s1", "image-" + "b" * 16 + ".png")

        assert artifact_store.resolve_artifact(uri, root=tmp_path) is None


class TestFindArtifact:
    def test_finds_stored_bytes_by_content(self, tmp_path):
        artifact_store.write_artifact("s1", _PNG, media_type="image", ext=".png", root=tmp_path)

        found = artifact_store.find_artifact("s1", _PNG, media_type="image", root=tmp_path)

        assert found is not None and found.read_bytes() == _PNG

    def test_missing_bytes_return_none(self, tmp_path):
        assert (
            artifact_store.find_artifact("s1", b"nope", media_type="image", root=tmp_path) is None
        )
