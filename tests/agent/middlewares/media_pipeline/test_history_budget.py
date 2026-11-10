"""Per-request history media budget (aligned with ZCode's 75 MiB projection).

The projection keeps images newest-first, protects the latest real user
message, and never mutates the checkpoint copy — only the request the model
sees.
"""

import asyncio
import base64

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.middlewares.media_pipeline import core as mm_mod
from config.features import MEDIA_PIPELINE

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


class _SentinelModel:
    def __init__(self, name: str = "main") -> None:
        self.model_name = name


def _data_url(size: int) -> str:
    return f"data:image/png;base64,{base64.b64encode(b'x' * size).decode()}"


def _image(size: int) -> dict:
    return {"type": "image_url", "image_url": {"url": _data_url(size)}}


def _human(text: str, sizes: list[int], *, user: bool = True) -> HumanMessage:
    content: list = [{"type": "text", "text": text}]
    content.extend(_image(size) for size in sizes)
    metadata = {"origin": "user"} if user else {}
    return HumanMessage(content=content, metadata=metadata)


def _tool(sizes: list[int]) -> ToolMessage:
    content: list = [{"type": "text", "text": "screenshot saved: /tmp/shot.png"}]
    content.extend(_image(size) for size in sizes)
    return ToolMessage(content=content, tool_call_id="call-1")


@pytest.fixture()
def processor() -> mm_mod.MultimodalProcessor:
    return mm_mod.MultimodalProcessor()


@pytest.fixture()
def budget(monkeypatch):
    def _set(bytes_limit: int, *, preserve: bool = True) -> None:
        monkeypatch.setitem(MEDIA_PIPELINE, "history_media_budget_bytes", bytes_limit)
        monkeypatch.setitem(MEDIA_PIPELINE, "preserve_latest_user_media", preserve)

    return _set


def _image_types(message) -> list[str]:
    return [item["type"] for item in message.content]


def _texts(message) -> str:
    return "\n".join(item.get("text", "") for item in message.content if item.get("type") == "text")


class TestHistoryBudgetProjection:
    def test_all_images_within_budget_are_kept(self, processor, budget):
        budget(10_000)
        messages = [_human(f"turn {i}", [100]) for i in range(3)]

        projected = processor._project_history_media_budget(messages)

        assert projected is messages
        assert all(_image_types(message) == ["text", "image_url"] for message in messages)

    def test_oversized_older_image_is_dropped_newest_first(self, processor, budget):
        budget(250)
        old = _human("old", [1000])
        tool = _tool([100])
        latest = _human("latest", [100])
        messages = [old, tool, latest]

        projected = processor._project_history_media_budget(messages)

        assert projected[0] is not old
        assert "image_url" not in _image_types(projected[0])
        assert "media budget" in _texts(projected[0])
        assert projected[1] is tool
        assert projected[2] is latest
        # The checkpoint copy is never rewritten.
        assert _image_types(old) == ["text", "image_url"]

    def test_latest_user_media_is_protected_even_over_budget(self, processor, budget):
        budget(50)
        old = _human("old", [100], user=False)
        latest = _human("latest", [1000])
        messages = [old, latest]

        projected = processor._project_history_media_budget(messages)

        assert "image_url" not in _image_types(projected[0])
        assert projected[1] is latest
        assert _image_types(latest) == ["text", "image_url"]

    def test_remote_urls_are_not_counted(self, processor, budget):
        budget(10)
        remote = HumanMessage(
            content=[
                {"type": "text", "text": "remote"},
                {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
            ]
        )
        latest = _human("latest", [1000])

        projected = processor._project_history_media_budget([remote, latest])

        assert projected[0] is remote
        assert projected[1] is latest

    def test_everything_is_dropped_when_budget_cannot_hold_any(self, processor, budget):
        budget(10, preserve=False)
        old = _human("old", [100])
        tool = _tool([200])
        messages = [old, tool]

        projected = processor._project_history_media_budget(messages)

        for message in projected:
            assert "image_url" not in _image_types(message)
            assert "media budget" in _texts(message)
        assert _image_types(old) == ["text", "image_url"]
        assert _image_types(tool) == ["text", "image_url"]

    def test_unstamped_transcript_protects_the_newest_media_human(self, processor, budget):
        budget(50)
        old = _human("old", [1000], user=False)
        latest = _human("latest", [100], user=False)

        projected = processor._project_history_media_budget([old, latest])

        assert "image_url" not in _image_types(projected[0])
        assert projected[1] is latest

    def test_request_override_leaves_the_original_request_untouched(self, processor, budget):
        budget(50, preserve=False)
        request = ModelRequest(
            model=_SentinelModel(),
            messages=[AIMessage(content="done"), _human("old", [100])],
        )

        projected = processor._project_budget_request(request)

        assert projected is not request
        assert "media budget" in _texts(projected.messages[1])
        assert _image_types(request.messages[1]) == ["text", "image_url"]

    def test_request_is_returned_unchanged_when_nothing_is_dropped(self, processor, budget):
        budget(10_000)
        request = ModelRequest(model=_SentinelModel(), messages=[_human("hi", [10])])

        assert processor._project_budget_request(request) is request

    def test_skill_path_mode_drops_even_the_latest_user_media(self, processor, budget, monkeypatch):
        budget(10_000)
        monkeypatch.setitem(MEDIA_PIPELINE, "main_llm_native_multimodal", "false")
        request = ModelRequest(model=_SentinelModel(), messages=[_human("hi", [10])])

        projected = processor._project_budget_request(request)

        assert "image_url" not in _image_types(projected.messages[0])
        assert "media budget" in _texts(projected.messages[0])

    def test_wrap_model_call_projects_before_the_handler(self, processor, budget):
        budget(50, preserve=False)
        request = ModelRequest(model=_SentinelModel(), messages=[_human("hi", [100])])
        seen: list[ModelRequest] = []

        def handler(inner: ModelRequest) -> AIMessage:
            seen.append(inner)
            return AIMessage(content="ok")

        processor.wrap_model_call(request, handler)

        assert len(seen) == 1
        assert "image_url" not in _image_types(seen[0].messages[0])

    def test_awrap_model_call_projects_before_the_handler(self, processor, budget):
        budget(50, preserve=False)
        request = ModelRequest(model=_SentinelModel(), messages=[_human("hi", [100])])
        seen: list[ModelRequest] = []

        async def handler(inner: ModelRequest) -> AIMessage:
            seen.append(inner)
            return AIMessage(content="ok")

        asyncio.run(processor.awrap_model_call(request, handler))

        assert len(seen) == 1
        assert "image_url" not in _image_types(seen[0].messages[0])
