"""Media input size limit: oversized payloads are skipped before any disk write.

``max_media_bytes`` gates every encoded payload and every downloaded URL body.
A rejected attachment never enters ``MediaPaths`` and never reaches the model as
a media block — the HumanMessage carries a text notice instead. A payload
exactly at the limit is allowed; a zero-byte or invalid payload keeps its
existing failure path.
"""

import base64

import pytest
from langchain_core.messages import HumanMessage

from agent.middlewares.media_pipeline import core as mm_mod
from agent.middlewares.media_pipeline import media_handlers
from config.features import MEDIA_PIPELINE

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SID = "size-session"
LIMIT = 4


@pytest.fixture(autouse=True)
def _small_limit(monkeypatch):
    monkeypatch.setitem(MEDIA_PIPELINE, "max_media_bytes", LIMIT)


@pytest.fixture()
def src_dir(tmp_path, monkeypatch):
    path = tmp_path / "src"
    monkeypatch.setattr(mm_mod, "SRC_DIR", path)
    return path


@pytest.fixture()
def processor(src_dir):
    return mm_mod.MultimodalProcessor()


def _state(messages: list) -> dict:
    return {"session_id": SID, "messages": messages}


def _media_files(src_dir) -> list[str]:
    media_dir = src_dir / SID / "media"
    if not media_dir.exists():
        return []
    return sorted(path.name for path in media_dir.iterdir())


class _FakeResponse:
    def __init__(self, body: bytes, content_length: str | None = None) -> None:
        self._body = body
        self.headers = {} if content_length is None else {"Content-Length": content_length}
        self.read_calls = 0

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self, n: int = -1) -> bytes:
        self.read_calls += 1
        if n is not None and n >= 0:
            return self._body[:n]
        return self._body


def _patch_urlopen(monkeypatch, response: _FakeResponse) -> None:
    monkeypatch.setattr(media_handlers.urllib.request, "urlopen", lambda *args, **kwargs: response)


class TestOversizeLocalPayloads:
    def test_over_limit_audio_bytes_are_skipped_without_writing(self, processor, src_dir):
        mes = HumanMessage(
            content=[
                {"type": "text", "text": "听音频"},
                {"type": "audio_bytes", "audio_bytes": b"\x49\x44\x33\x03\x00"},
            ]
        )

        processor._before_agent_impl(_state([mes]))

        assert [item["type"] for item in mes.content] == ["text"]
        assert "skipped" in mes.content[0]["text"]
        assert "exceeds" in mes.content[0]["text"]
        assert "audios" not in mes.additional_kwargs
        assert _media_files(src_dir) == []

    def test_over_limit_base64_image_is_skipped_without_writing(self, processor, src_dir):
        payload = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("utf-8")
        mes = HumanMessage(
            content=[
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{payload}"}},
            ]
        )

        processor._before_agent_impl(_state([mes]))

        assert [item["type"] for item in mes.content] == ["text"]
        assert "exceeds" in mes.content[0]["text"]
        assert "images" not in mes.additional_kwargs
        assert _media_files(src_dir) == []

    def test_payload_exactly_at_limit_is_allowed(self, processor, src_dir):
        mes = HumanMessage(
            content=[
                {"type": "text", "text": "听音频"},
                {"type": "audio_bytes", "audio_bytes": b"\x49\x44\x33\x03"},
            ]
        )

        processor._before_agent_impl(_state([mes]))

        assert [item["type"] for item in mes.content] == ["text", "audio_bytes"]
        assert len(mes.additional_kwargs["audios"]) == 1
        assert len(_media_files(src_dir)) == 1


class TestOversizeRemoteUrls:
    def test_declared_content_length_over_limit_skips_before_reading(
        self, processor, src_dir, monkeypatch
    ):
        response = _FakeResponse(b"", content_length="100")
        _patch_urlopen(monkeypatch, response)
        mes = HumanMessage(
            content=[
                {"type": "text", "text": "听音频"},
                {"type": "audio_url", "audio_url": {"url": "http://example.com/a.mp3"}},
            ]
        )

        processor._before_agent_impl(_state([mes]))

        assert response.read_calls == 0
        assert [item["type"] for item in mes.content] == ["text"]
        assert "100 bytes" in mes.content[0]["text"]
        assert "audios" not in mes.additional_kwargs
        assert _media_files(src_dir) == []

    def test_capped_read_over_limit_is_skipped(self, processor, src_dir, monkeypatch):
        _patch_urlopen(monkeypatch, _FakeResponse(b"12345678"))
        mes = HumanMessage(
            content=[
                {"type": "text", "text": "看视频"},
                {"type": "video_url", "video_url": {"url": "http://example.com/v.mp4"}},
            ]
        )

        processor._before_agent_impl(_state([mes]))

        assert [item["type"] for item in mes.content] == ["text"]
        assert "exceeds" in mes.content[0]["text"]
        assert "videos" not in mes.additional_kwargs
        assert _media_files(src_dir) == []

    def test_empty_remote_body_follows_existing_failure_path(self, processor, src_dir, monkeypatch):
        _patch_urlopen(monkeypatch, _FakeResponse(b""))
        mes = HumanMessage(
            content=[
                {"type": "text", "text": "听音频"},
                {"type": "audio_url", "audio_url": {"url": "http://example.com/a.mp3"}},
            ]
        )

        processor._before_agent_impl(_state([mes]))

        assert [item["type"] for item in mes.content] == ["text"]
        assert "skipped" not in mes.content[0]["text"]
        assert "audios" not in mes.additional_kwargs
        assert _media_files(src_dir) == []


class TestInvalidPayload:
    def test_invalid_image_payload_follows_existing_failure_path(self, processor, src_dir):
        mes = HumanMessage(
            content=[
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]
        )

        processor._before_agent_impl(_state([mes]))

        assert [item["type"] for item in mes.content] == ["text"]
        assert "skipped" not in mes.content[0]["text"]
        assert "images" not in mes.additional_kwargs
        assert _media_files(src_dir) == []
