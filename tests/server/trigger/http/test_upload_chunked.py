"""Chunked upload: staging, assembly by content hash, caps and dedupe."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

import server.trigger.http.upload_chunked as upload_chunked
from config.features import HTTP_UPLOAD

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


class _Request:
    def __init__(self, query: dict, body: bytes = b""):
        self.query_params = query
        self.body = body
        self.headers = {"Content-Type": "image/png"}


def _call(handler, query: dict, body: bytes = b""):
    return asyncio.run(handler(_Request(query, body)))


def _payload(response) -> dict:
    return json.loads(response.description)


def _chunk(
    session_id: str,
    upload_id: str,
    index: int,
    total: int,
    data: bytes,
    *,
    content_type: str = "image/png",
    filename: str = "shot.png",
):
    return _call(
        upload_chunked.upload_chunk_handler,
        {
            "session_id": session_id,
            "upload_id": upload_id,
            "chunk_index": str(index),
            "total_chunks": str(total),
            "filename": filename,
            "content_type": content_type,
        },
        data,
    )


def _complete(session_id: str, upload_id: str):
    return _call(
        upload_chunked.complete_upload_handler,
        {"session_id": session_id, "upload_id": upload_id},
    )


@pytest.fixture()
def upload_root(tmp_path, monkeypatch):
    source = tmp_path / "src"
    temp = tmp_path / "temp"
    monkeypatch.setattr(upload_chunked, "SRC_DIR", source)
    monkeypatch.setattr(upload_chunked, "TEMP_DIR", temp)
    monkeypatch.setattr(upload_chunked, "API_HOST", "127.0.0.1")
    monkeypatch.setattr(upload_chunked, "API_PORT", 8080)
    return source, temp


def test_two_chunks_assemble_into_one_content_addressed_file(upload_root):
    source, temp = upload_root
    payload = b"abc" + b"def"

    assert int(_chunk("s1", "u1", 0, 2, b"abc").status_code) == 200
    assert int(_chunk("s1", "u1", 1, 2, b"def").status_code) == 200

    response = _complete("s1", "u1")

    assert int(response.status_code) == 200
    body = _payload(response)
    digest = hashlib.sha256(payload).hexdigest()
    assert body["sha256"] == digest
    assert body["filename"] == f"{digest[:16]}.png"
    assert body["url"] == f"http://127.0.0.1:8080/images/{digest[:16]}.png"
    assert (source / "images" / body["filename"]).read_bytes() == payload
    assert not (temp / "upload_chunks" / "s1" / "u1").exists()


def test_an_incomplete_upload_is_refused_and_keeps_its_staging(upload_root):
    source, temp = upload_root
    _chunk("s1", "u2", 0, 2, b"abc")

    response = _complete("s1", "u2")

    assert int(response.status_code) == 400
    assert "missing chunk 1" in _payload(response)["message"]
    assert not (source / "images").exists()
    assert (temp / "upload_chunks" / "s1" / "u2" / "0").is_file()


def test_identical_bytes_reuse_the_same_file_and_url(upload_root):
    source, _temp = upload_root
    payload = b"same-bytes"
    for upload_id in ("u3", "u4"):
        _chunk("s1", upload_id, 0, 1, payload)
    first = _payload(_complete("s1", "u3"))
    target = source / "images" / first["filename"]
    mtime = target.stat().st_mtime_ns

    second = _payload(_complete("s1", "u4"))

    assert second["url"] == first["url"]
    assert second["filename"] == first["filename"]
    assert target.stat().st_mtime_ns == mtime


def test_a_chunk_over_the_chunk_size_is_refused(upload_root, monkeypatch):
    monkeypatch.setitem(HTTP_UPLOAD, "chunk_size_bytes", 2)

    response = _chunk("s1", "u5", 0, 1, b"abc")

    assert int(response.status_code) == 413
    assert "chunk exceeds" in _payload(response)["message"]


def test_the_final_file_respects_the_per_kind_cap(upload_root, monkeypatch):
    monkeypatch.setitem(upload_chunked._KIND_MAX, "image", 2)
    _chunk("s1", "u6", 0, 1, b"abc")

    response = _complete("s1", "u6")

    assert int(response.status_code) == 413


def test_unsafe_or_unknown_parameters_are_refused(upload_root):
    assert int(_chunk("../escape", "u7", 0, 1, b"x").status_code) == 400
    assert int(_chunk("s1", "../escape", 0, 1, b"x").status_code) == 400
    assert int(_chunk("s1", "u8", 1, 1, b"x").status_code) == 400
    assert int(_chunk("s1", "u8", 0, 1, b"x", content_type="text/plain").status_code) == 400
    assert int(_complete("s1", "missing").status_code) == 404


def test_audio_uploads_land_in_the_audio_directory(upload_root):
    source, _temp = upload_root
    _chunk("s1", "u9", 0, 1, b"RIFFxxxx", content_type="audio/wav", filename="a.wav")

    body = _payload(_complete("s1", "u9"))

    assert "/audio/" in body["url"]
    assert body["filename"].endswith(".wav")
    assert (source / "audio" / body["filename"]).is_file()
