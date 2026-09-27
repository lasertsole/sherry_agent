"""SSRF guard at the media boundary: private targets are never fetched.

A user-supplied media URL must not turn the media pipeline into an
internal-network reader (cloud metadata, loopback services): the refusal
happens before any request goes out, writes no files, and is recorded as a
model-visible skipped notice. A public host still passes through — downloads
for audio/video, and a passthrough block for images.
"""

from __future__ import annotations

import socket

import pytest
from langchain_core.messages import HumanMessage

from agent.middlewares.media_pipeline import core as mm_mod
from agent.middlewares.media_pipeline import media_handlers
from pub.func.validator import public_url as guard_mod

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SID = "ssrf-session"


@pytest.fixture(autouse=True)
def _no_escape_hatch(monkeypatch):
    monkeypatch.delenv("SHERRY_ALLOW_PRIVATE_MEDIA_URLS", raising=False)


@pytest.fixture()
def src_dir(tmp_path, monkeypatch):
    path = tmp_path / "src"
    monkeypatch.setattr(mm_mod, "SRC_DIR", path)
    return path


@pytest.fixture()
def processor(src_dir):
    return mm_mod.MultimodalProcessor()


def _patch_dns(monkeypatch, *addresses: str) -> None:
    infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)) for addr in addresses]
    monkeypatch.setattr(guard_mod.socket, "getaddrinfo", lambda *args, **kwargs: infos)


def _state(messages: list) -> dict:
    return {"session_id": SID, "messages": messages}


def _media_files(src_dir) -> list[str]:
    media_dir = src_dir / SID / "media"
    return sorted(path.name for path in media_dir.iterdir()) if media_dir.exists() else []


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.headers: dict[str, str] = {}

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self, n: int = -1) -> bytes:
        return self._body if n is None or n < 0 else self._body[:n]


def test_audio_url_to_cloud_metadata_is_refused_before_any_request(processor, src_dir, monkeypatch):
    _patch_dns(monkeypatch, "169.254.169.254")
    calls: list = []
    monkeypatch.setattr(
        media_handlers.urllib.request, "urlopen", lambda *args, **kwargs: calls.append(args)
    )
    mes = HumanMessage(
        content=[
            {"type": "text", "text": "听音频"},
            {"type": "audio_url", "audio_url": {"url": "http://metadata.internal/a.mp3"}},
        ]
    )

    processor._before_agent_impl(_state([mes]))

    assert calls == [], "a private target must never be fetched"
    assert [item["type"] for item in mes.content] == ["text"]
    assert "non-public address" in mes.content[0]["text"]
    assert "audios" not in mes.additional_kwargs
    assert _media_files(src_dir) == []


def test_video_url_to_rfc1918_is_refused(processor, src_dir, monkeypatch):
    _patch_dns(monkeypatch, "10.1.2.3")
    mes = HumanMessage(
        content=[
            {"type": "text", "text": "看视频"},
            {"type": "video_url", "video_url": {"url": "http://nas.lan/v.mp4"}},
        ]
    )

    processor._before_agent_impl(_state([mes]))

    assert "non-public address" in mes.content[0]["text"]
    assert "videos" not in mes.additional_kwargs
    assert _media_files(src_dir) == []


def test_image_url_to_loopback_is_refused(processor, src_dir, monkeypatch):
    _patch_dns(monkeypatch, "127.0.0.1")
    mes = HumanMessage(
        content=[
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "http://127.0.0.1:8080/x.png"}},
        ]
    )

    processor._before_agent_impl(_state([mes]))

    assert [item["type"] for item in mes.content] == ["text"]
    assert "non-public address" in mes.content[0]["text"]
    assert "images" not in mes.additional_kwargs
    assert _media_files(src_dir) == []


def test_public_audio_url_reaches_the_transport(processor, src_dir, monkeypatch):
    _patch_dns(monkeypatch, "93.184.216.34")
    monkeypatch.setattr(
        media_handlers.urllib.request, "urlopen", lambda *args, **kwargs: _FakeResponse(b"RIFF")
    )
    mes = HumanMessage(
        content=[
            {"type": "text", "text": "听音频"},
            {"type": "audio_url", "audio_url": {"url": "https://example.com/a.wav"}},
        ]
    )

    processor._before_agent_impl(_state([mes]))

    assert _media_files(src_dir), "a public host still downloads"
    assert "non-public" not in mes.content[0]["text"]


def test_public_image_url_still_passes_through(processor, src_dir, monkeypatch):
    _patch_dns(monkeypatch, "93.184.216.34")
    mes = HumanMessage(
        content=[
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "https://example.com/pic.png"}},
        ]
    )

    processor._before_agent_impl(_state([mes]))

    assert any(item.get("type") == "image_url" for item in mes.content)
    assert "non-public" not in str(mes.content[0].get("text") or "")
    assert _media_files(src_dir) == []


def test_escape_hatch_allows_a_loopback_media_server(processor, src_dir, monkeypatch):
    _patch_dns(monkeypatch, "127.0.0.1")
    monkeypatch.setenv("SHERRY_ALLOW_PRIVATE_MEDIA_URLS", "1")
    monkeypatch.setattr(
        media_handlers.urllib.request, "urlopen", lambda *args, **kwargs: _FakeResponse(b"RIFF")
    )
    mes = HumanMessage(
        content=[
            {"type": "text", "text": "听音频"},
            {"type": "audio_url", "audio_url": {"url": "http://127.0.0.1:9000/a.wav"}},
        ]
    )

    processor._before_agent_impl(_state([mes]))

    assert _media_files(src_dir), "the documented escape hatch skips the guard"
