"""Compression-time inline-media offload: sync/async parity and safety.

Covers ``agent/middlewares/summarization/media_offload.py`` directly plus its
wiring into both compression paths of ``Summarization``:
media in the summarized prefix is written to the session media dir (content
hash, deduplicated) and replaced by an ``[evicted to: <path>]`` pointer that
``SummaryDoc.evicted_refs`` collects; the preserved tail keeps its media; every
failure degrades to the placeholder.
"""

import asyncio
import base64
import hashlib
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import agent.middlewares.summarization.core as summarization_module
import agent.middlewares.summarization.media_offload as media_offload
from agent.middlewares.summarization.media_offload import (
    MEDIA_FAILED_PLACEHOLDER,
    offload_inline_media,
)
from agent.middlewares.summarization.summary_doc import SummaryDoc
from runtime import state_register_mem

pytestmark = [pytest.mark.module]

_PNG = b"\x89PNG\r\n\x1a\n" + b"image-body"
_WAV = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVEfmt "


def _data_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _image_block(url: str) -> dict:
    return {"type": "image_url", "image_url": {"url": url}}


class _RunnableStub:
    def __init__(self, result):
        self.result = result
        self.prompts: list[str] = []

    def invoke(self, prompt, config=None):
        self.prompts.append(prompt)
        return self.result

    async def ainvoke(self, prompt, config=None):
        self.prompts.append(prompt)
        return self.result


class _StubModel:
    _llm_type = "fake"

    def __init__(self, result=None):
        self.runnable = _RunnableStub(result or SummaryDoc(goal="g"))

    def with_structured_output(self, schema, *, method=None, **kwargs):
        return self.runnable

    def invoke(self, prompt, config=None):
        return SimpleNamespace(text="")


def _make_middleware(model=None) -> summarization_module.Summarization:
    return summarization_module.Summarization(
        model=model or _StubModel(), trigger=[("tokens", 10**9)]
    )


def _request(messages, session_id):
    from langchain.agents.middleware import ModelRequest

    return ModelRequest(
        model=_StubModel(),
        messages=list(messages),
        state={"session_id": session_id, "messages": list(messages)},
    )


def _media_files(root: Path, session_id: str) -> list[Path]:
    directory = root / session_id / media_offload.MEDIA_OFFLOAD_SUBDIR
    return sorted(directory.glob("*")) if directory.exists() else []


@pytest.fixture
def sessions_root(tmp_path, monkeypatch):
    monkeypatch.setattr(media_offload, "SESSIONS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def sid(request):
    session = "med-" + request.node.name[:36] + "-" + uuid.uuid4().hex[:6]
    yield session
    state_register_mem.clear_session(session)


class TestOffloadInlineMedia:
    def test_inline_image_is_written_and_replaced_by_pointer(self, sessions_root, sid):
        raw = _PNG
        message = HumanMessage(
            content=[{"type": "text", "text": "look"}, _image_block(_data_url(raw))]
        )

        rewritten = offload_inline_media([message], sid)

        assert message.content[1]["type"] == "image_url"
        pointer = rewritten[0].content[1]
        assert pointer["type"] == "text"
        expected_name = hashlib.sha256(raw).hexdigest()[:16] + ".png"
        assert pointer["text"] == f"[evicted to: {sessions_root / sid / 'media' / expected_name}]"
        assert (sessions_root / sid / "media" / expected_name).read_bytes() == raw

    def test_non_media_blocks_are_preserved(self, sessions_root, sid):
        message = HumanMessage(content=[{"type": "text", "text": "hi"}, {"type": "weird"}])
        assert offload_inline_media([message], sid)[0] is message

    def test_identical_media_is_written_once(self, sessions_root, sid):
        url = _data_url(_PNG)
        messages = [
            HumanMessage(content=[_image_block(url)]),
            HumanMessage(content=[_image_block(url)]),
        ]

        rewritten = offload_inline_media(messages, sid)

        names = {path.name for path in _media_files(sessions_root, sid)}
        assert len(names) == 1
        assert rewritten[0].content[0]["text"] == rewritten[1].content[0]["text"]

    def test_remote_url_is_not_offloaded(self, sessions_root, sid):
        message = HumanMessage(content=[_image_block("https://example.test/a.png")])
        assert offload_inline_media([message], sid)[0] is message
        assert _media_files(sessions_root, sid) == []

    def test_audio_bytes_block_is_written(self, sessions_root, sid):
        message = HumanMessage(content=[{"type": "audio_bytes", "audio_bytes": {"bytes": _WAV}}])
        pointer = offload_inline_media([message], sid)[0].content[0]
        assert pointer["text"].startswith("[evicted to: ")
        assert pointer["text"].endswith(".wav]")

    def test_malformed_data_url_becomes_placeholder(self, sessions_root, sid):
        message = HumanMessage(content=[_image_block("data:image/png;base64")])
        block = offload_inline_media([message], sid)[0].content[0]
        assert block == {"type": "text", "text": MEDIA_FAILED_PLACEHOLDER}

    def test_store_failure_becomes_placeholder(self, sessions_root, sid, monkeypatch):
        monkeypatch.setattr(media_offload, "_store_media", lambda *args: None)
        message = HumanMessage(content=[_image_block(_data_url(_PNG))])
        block = offload_inline_media([message], sid)[0].content[0]
        assert block == {"type": "text", "text": MEDIA_FAILED_PLACEHOLDER}

    def test_unsafe_session_id_becomes_placeholder(self, sessions_root):
        message = HumanMessage(content=[_image_block(_data_url(_PNG))])
        block = offload_inline_media([message], "../escape")[0].content[0]
        assert block == {"type": "text", "text": MEDIA_FAILED_PLACEHOLDER}


def _disable_nudges(monkeypatch):
    monkeypatch.setattr(summarization_module, "_schedule_compression_nudges", lambda *a: None)
    monkeypatch.setattr(summarization_module, "_schedule_compression_todo_update", lambda *a: None)


def _summary_doc_of(final_messages):
    for message in final_messages:
        if isinstance(message, AIMessage) and "summary_doc" in message.additional_kwargs:
            return message.additional_kwargs["summary_doc"]
    raise AssertionError("no summary_doc on the compressed messages")


class TestCompressionIntegration:
    def test_sync_compression_offloads_prefix_and_records_ref(
        self, sessions_root, sid, monkeypatch
    ):
        _disable_nudges(monkeypatch)
        model = _StubModel()
        mw = _make_middleware(model)
        media_msg = HumanMessage(content=[_image_block(_data_url(_PNG))])
        messages = [media_msg, AIMessage(content="ok"), HumanMessage(content="tail question")]
        monkeypatch.setattr(mw, "_determine_cutoff", lambda msgs: 2)

        result = mw._apply_compression_under_lock(_request(messages, sid), sid)

        final = list(result.messages)
        assert final[0].content == "What did we do so far?"
        assert len(_media_files(sessions_root, sid)) == 1
        path = str(_media_files(sessions_root, sid)[0])
        assert path in model.runnable.prompts[0]
        assert path in _summary_doc_of(final)["evicted_refs"]

    def test_async_compression_offloads_prefix_and_records_ref(
        self, sessions_root, sid, monkeypatch
    ):
        _disable_nudges(monkeypatch)
        model = _StubModel()
        mw = _make_middleware(model)
        media_msg = HumanMessage(content=[_image_block(_data_url(_PNG))])
        messages = [media_msg, AIMessage(content="ok"), HumanMessage(content="tail question")]
        monkeypatch.setattr(mw, "_determine_cutoff", lambda msgs: 2)

        result = asyncio.run(mw._aapply_compression_under_lock(_request(messages, sid), sid))

        final = list(result.messages)
        assert len(_media_files(sessions_root, sid)) == 1
        path = str(_media_files(sessions_root, sid)[0])
        assert path in model.runnable.prompts[0]
        assert path in _summary_doc_of(final)["evicted_refs"]

    def test_preserved_window_media_is_untouched(self, sessions_root, sid, monkeypatch):
        _disable_nudges(monkeypatch)
        model = _StubModel()
        mw = _make_middleware(model)
        media_msg = HumanMessage(content=[_image_block(_data_url(_PNG))])
        messages = [HumanMessage(content="q1"), AIMessage(content="a1"), media_msg]
        monkeypatch.setattr(mw, "_determine_cutoff", lambda msgs: 2)

        result = mw._apply_compression_under_lock(_request(messages, sid), sid)

        assert result.messages[-1].content[0]["type"] == "image_url"
        assert _media_files(sessions_root, sid) == []

    def test_failed_offload_keeps_compression_alive(self, sessions_root, sid, monkeypatch):
        _disable_nudges(monkeypatch)
        monkeypatch.setattr(media_offload, "_store_media", lambda *args: None)
        model = _StubModel()
        mw = _make_middleware(model)
        media_msg = HumanMessage(content=[_image_block(_data_url(_PNG))])
        messages = [media_msg, AIMessage(content="ok"), HumanMessage(content="tail")]
        monkeypatch.setattr(mw, "_determine_cutoff", lambda msgs: 2)

        result = mw._apply_compression_under_lock(_request(messages, sid), sid)

        final = list(result.messages)
        assert MEDIA_FAILED_PLACEHOLDER in model.runnable.prompts[0]
        assert _summary_doc_of(final)["evicted_refs"] == []
