"""Chunked upload: begin/status/abort, sequential chunks, hash dedupe, caps."""

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


def _begin(
    session_id: str,
    total_bytes: int,
    *,
    total_chunks: int | None = None,
    content_type: str = "image/png",
    filename: str = "shot.png",
):
    return _call(
        upload_chunked.begin_upload_handler,
        {
            "session_id": session_id,
            "filename": filename,
            "content_type": content_type,
            "total_chunks": str(
                total_chunks if total_chunks is not None else max(1, total_bytes // 4 + 1)
            ),
            "total_bytes": str(total_bytes),
        },
    )


def _chunk(upload_id: str, index: int, data: bytes):
    return _call(
        upload_chunked.upload_chunk_handler,
        {"upload_id": upload_id, "chunk_index": str(index)},
        data,
    )


def _complete(upload_id: str):
    return _call(upload_chunked.complete_upload_handler, {"upload_id": upload_id})


def _status(upload_id: str):
    return _call(upload_chunked.status_upload_handler, {"upload_id": upload_id})


def _abort(upload_id: str):
    return _call(upload_chunked.abort_upload_handler, {"upload_id": upload_id})


@pytest.fixture()
def upload_root(tmp_path, monkeypatch):
    source = tmp_path / "src"
    temp = tmp_path / "temp"
    monkeypatch.setattr(upload_chunked, "SRC_DIR", source)
    monkeypatch.setattr(upload_chunked, "TEMP_DIR", temp)
    monkeypatch.setattr(upload_chunked, "API_HOST", "127.0.0.1")
    monkeypatch.setattr(upload_chunked, "API_PORT", 8080)
    return source, temp


def test_begin_sequential_chunks_and_complete(upload_root, monkeypatch):
    source, temp = upload_root
    monkeypatch.setitem(HTTP_UPLOAD, "chunk_size_bytes", 4)
    begun = _payload(_begin("s1", 6, total_chunks=2))
    upload_id = begun["upload_id"]
    assert begun["next_chunk_index"] == 0

    assert _payload(_chunk(upload_id, 0, b"abc"))["next_chunk_index"] == 1
    assert _payload(_chunk(upload_id, 1, b"def"))["next_chunk_index"] == 2
    assert _payload(_status(upload_id)) == {
        "ok": True,
        "received_chunks": 2,
        "total_chunks": 2,
    }

    response = _complete(upload_id)

    assert int(response.status_code) == 200
    body = _payload(response)
    digest = hashlib.sha256(b"abcdef").hexdigest()
    assert body["sha256"] == digest
    assert body["filename"] == f"{digest[:16]}.png"
    assert body["url"] == f"http://127.0.0.1:8080/images/{digest[:16]}.png"
    assert (source / "images" / body["filename"]).read_bytes() == b"abcdef"
    assert not (temp / "upload_chunks" / upload_id).exists()


def test_out_of_order_and_replayed_chunks_are_refused(upload_root, monkeypatch):
    _source, temp = upload_root
    monkeypatch.setitem(HTTP_UPLOAD, "chunk_size_bytes", 4)
    upload_id = _payload(_begin("s1", 6, total_chunks=2))["upload_id"]

    gap = _chunk(upload_id, 1, b"def")
    assert int(gap.status_code) == 409
    assert _payload(gap)["code"] == "chunkGap"

    _chunk(upload_id, 0, b"abc")
    conflict = _chunk(upload_id, 0, b"abc")
    assert int(conflict.status_code) == 409
    assert _payload(conflict)["code"] == "chunkConflict"
    assert (temp / "upload_chunks" / upload_id / "0").is_file()


def test_an_incomplete_upload_is_refused_and_keeps_its_staging(upload_root, monkeypatch):
    source, temp = upload_root
    monkeypatch.setitem(HTTP_UPLOAD, "chunk_size_bytes", 4)
    upload_id = _payload(_begin("s1", 6, total_chunks=2))["upload_id"]
    _chunk(upload_id, 0, b"abc")

    response = _complete(upload_id)

    assert int(response.status_code) == 400
    assert _payload(response)["code"] == "chunkGap"
    assert not (source / "images").exists()
    assert (temp / "upload_chunks" / upload_id / "0").is_file()


def test_identical_bytes_reuse_the_same_file_and_url(upload_root, monkeypatch):
    source, _temp = upload_root
    monkeypatch.setitem(HTTP_UPLOAD, "chunk_size_bytes", 4)
    payload = b"same-bytes"
    urls = []
    for _ in range(2):
        upload_id = _payload(_begin("s1", len(payload), total_chunks=3))["upload_id"]
        _chunk(upload_id, 0, payload[:4])
        _chunk(upload_id, 1, payload[4:8])
        _chunk(upload_id, 2, payload[8:])
        urls.append(_payload(_complete(upload_id)))
    target = source / "images" / urls[0]["filename"]
    mtime = target.stat().st_mtime_ns

    assert urls[1]["url"] == urls[0]["url"]
    assert target.stat().st_mtime_ns == mtime


def test_a_chunk_over_the_chunk_size_is_refused(upload_root, monkeypatch):
    monkeypatch.setitem(HTTP_UPLOAD, "chunk_size_bytes", 2)
    upload_id = _payload(_begin("s1", 3, total_chunks=2))["upload_id"]

    response = _chunk(upload_id, 0, b"abc")

    assert int(response.status_code) == 413
    assert "chunk exceeds" in _payload(response)["message"]


def test_chunks_cannot_exceed_the_declared_total(upload_root, monkeypatch):
    monkeypatch.setitem(HTTP_UPLOAD, "chunk_size_bytes", 4)
    upload_id = _payload(_begin("s1", 5, total_chunks=2))["upload_id"]
    assert int(_chunk(upload_id, 0, b"abcd").status_code) == 200

    response = _chunk(upload_id, 1, b"abcd")

    assert int(response.status_code) == 413
    assert "declared size" in _payload(response)["message"]


def test_begin_validates_the_declared_size_and_type(upload_root, monkeypatch):
    monkeypatch.setitem(HTTP_UPLOAD, "chunk_size_bytes", 4)
    monkeypatch.setitem(upload_chunked._KIND_MAX, "image", 2)
    assert int(_begin("s1", 3, total_chunks=1).status_code) == 413
    monkeypatch.setitem(upload_chunked._KIND_MAX, "image", 25 * 1024 * 1024)
    assert int(_begin("../escape", 3, total_chunks=1).status_code) == 400
    assert int(_begin("s1", 3, total_chunks=9).status_code) == 400
    bad_type = _begin("s1", 3, total_chunks=1, content_type="text/plain")
    assert int(bad_type.status_code) == 400


def test_status_and_abort(upload_root, monkeypatch):
    _source, temp = upload_root
    monkeypatch.setitem(HTTP_UPLOAD, "chunk_size_bytes", 4)
    upload_id = _payload(_begin("s1", 6, total_chunks=2))["upload_id"]
    _chunk(upload_id, 0, b"abc")
    assert _payload(_status(upload_id))["received_chunks"] == 1

    assert _payload(_abort(upload_id)) == {"ok": True}
    assert not (temp / "upload_chunks" / upload_id).exists()
    assert int(_status(upload_id).status_code) == 404
    assert _payload(_abort(upload_id)) == {"ok": True}


def test_audio_uploads_land_in_the_audio_directory(upload_root, monkeypatch):
    source, _temp = upload_root
    monkeypatch.setitem(HTTP_UPLOAD, "chunk_size_bytes", 8)
    upload_id = _payload(
        _begin("s1", 8, total_chunks=1, content_type="audio/wav", filename="a.wav")
    )["upload_id"]
    _chunk(upload_id, 0, b"RIFFxxxx")

    body = _payload(_complete(upload_id))

    assert "/audio/" in body["url"]
    assert body["filename"].endswith(".wav")
    assert (source / "audio" / body["filename"]).is_file()
