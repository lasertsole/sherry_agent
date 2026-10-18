"""Streaming download helpers used by the 模型配置 local-model prefetch."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from models.downloader import probe_size, stream_download

pytestmark = [pytest.mark.unit]


class _Response:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        status_code: int = 200,
        length: int | None = None,
    ) -> None:
        self.status_code = status_code
        self._chunks = chunks
        self.headers = {} if length is None else {"content-length": str(length)}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.invalid/model.gguf")
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )

    def iter_bytes(self, chunk_size: int) -> list[bytes]:
        assert chunk_size > 0
        return self._chunks


class _Stream:
    def __init__(self, response: _Response) -> None:
        self._response = response

    def __enter__(self) -> _Response:
        return self._response

    def __exit__(self, *args: object) -> None:
        return None


def _patch_stream(monkeypatch, response: _Response) -> dict[str, object]:
    calls: dict[str, object] = {}

    def fake_stream(method: str, url: str, **kwargs: object) -> _Stream:
        calls["method"] = method
        calls["url"] = url
        calls.update(kwargs)
        return _Stream(response)

    monkeypatch.setattr(httpx, "stream", fake_stream)
    return calls


def test_probe_size_returns_content_length(monkeypatch) -> None:
    monkeypatch.setattr(
        httpx,
        "head",
        lambda *args, **kwargs: _Response([], length=123),
    )

    assert probe_size("https://example.invalid/model.gguf", 5) == 123


def test_probe_size_is_zero_on_failure(monkeypatch) -> None:
    def fail(*args: object, **kwargs: object) -> _Response:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "head", fail)

    assert probe_size("https://example.invalid/model.gguf", 5) == 0


def test_stream_download_writes_the_part_then_replaces(tmp_path: Path, monkeypatch) -> None:
    calls = _patch_stream(monkeypatch, _Response([b"abc", b"def"], length=6))
    progress: list[tuple[int, int]] = []
    dest = tmp_path / "nested" / "model.gguf"

    written = stream_download(
        "https://example.invalid/model.gguf",
        dest,
        timeout_seconds=5,
        chunk_bytes=3,
        progress=lambda done, total: progress.append((done, total)),
    )

    assert written == 6
    assert dest.read_bytes() == b"abcdef"
    assert not dest.with_name("model.gguf.part").exists()
    assert calls["headers"] == {}
    assert progress == [(0, 6), (3, 6), (6, 6)]


def test_stream_download_resumes_a_partial_file_with_range(tmp_path: Path, monkeypatch) -> None:
    dest = tmp_path / "model.gguf"
    part = dest.with_name("model.gguf.part")
    part.write_bytes(b"abc")
    calls = _patch_stream(monkeypatch, _Response([b"de", b"fg"], status_code=206, length=4))
    progress: list[tuple[int, int]] = []

    written = stream_download(
        "https://example.invalid/model.gguf",
        dest,
        timeout_seconds=5,
        chunk_bytes=2,
        progress=lambda done, total: progress.append((done, total)),
    )

    assert written == 7
    assert dest.read_bytes() == b"abcdefg"
    assert not part.exists()
    assert calls["headers"] == {"Range": "bytes=3-"}
    # The advertised total counts the prefix the resumed file already holds.
    assert progress == [(3, 7), (5, 7), (7, 7)]


def test_stream_download_restarts_when_the_server_ignores_range(
    tmp_path: Path, monkeypatch
) -> None:
    dest = tmp_path / "model.gguf"
    dest.with_name("model.gguf.part").write_bytes(b"stale")
    calls = _patch_stream(monkeypatch, _Response([b"xy"], status_code=200, length=2))

    written = stream_download(
        "https://example.invalid/model.gguf",
        dest,
        timeout_seconds=5,
        chunk_bytes=2,
    )

    assert written == 2
    assert dest.read_bytes() == b"xy"
    assert calls["headers"] == {"Range": "bytes=5-"}


def test_stream_download_keeps_the_part_on_failure(tmp_path: Path, monkeypatch) -> None:
    class _BrokenResponse(_Response):
        def iter_bytes(self, chunk_size: int) -> list[bytes]:
            raise httpx.ReadError("connection dropped")

    def broken_stream(*args: object, **kwargs: object) -> _Stream:
        return _Stream(_BrokenResponse([], status_code=206, length=3))

    monkeypatch.setattr(httpx, "stream", broken_stream)
    dest = tmp_path / "model.gguf"
    part = dest.with_name("model.gguf.part")
    part.write_bytes(b"partial")

    with pytest.raises(httpx.ReadError):
        stream_download(
            "https://example.invalid/model.gguf",
            dest,
            timeout_seconds=5,
            chunk_bytes=3,
        )

    assert part.read_bytes() == b"partial"
    assert not dest.exists()
