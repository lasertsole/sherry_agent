"""Shared history-media byte budget: pure projection, middleware and both chains.

The projection used to live only inside ``MultimodalProcessor`` (main profile).
It is now a profile-neutral pure function plus a ``wrap_model_call`` layer that
the spec table registers in BOTH the main and child chains, so a subagent that
reads several images cannot accumulate unbounded media history.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.middlewares.media_pipeline.budget_projection import (
    MediaBudgetProjectionMiddleware,
    project_media_budget,
)
from config.features import MEDIA_PIPELINE

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


class _SentinelModel:
    def __init__(self, name: str = "main") -> None:
        self.model_name = name


def _data_url(size: int) -> str:
    return f"data:image/png;base64,{base64.b64encode(b'x' * size).decode()}"


def _media(block_type: str, size: int) -> dict:
    return {"type": block_type, block_type: {"url": _data_url(size)}}


def _image(size: int) -> dict:
    return _media("image_url", size)


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
def budget(monkeypatch):
    def _set(bytes_limit: int, *, preserve: bool = True) -> None:
        monkeypatch.setitem(MEDIA_PIPELINE, "history_media_budget_bytes", bytes_limit)
        monkeypatch.setitem(MEDIA_PIPELINE, "preserve_latest_user_media", preserve)

    return _set


def _types(message) -> list[str]:
    return [item["type"] for item in message.content]


def _texts(message) -> str:
    return "\n".join(item.get("text", "") for item in message.content if item.get("type") == "text")


class TestPureProjection:
    def test_all_media_within_budget_is_kept(self, budget):
        budget(10_000)
        messages = [_human(f"turn {i}", [100]) for i in range(3)]

        assert project_media_budget(messages) is messages
        assert all(_types(message) == ["text", "image_url"] for message in messages)

    def test_oversized_older_image_is_dropped_newest_first(self, budget):
        budget(250)
        old = _human("old", [1000])
        tool = _tool([100])
        latest = _human("latest", [100])
        messages = [old, tool, latest]

        projected = project_media_budget(messages)

        assert projected[0] is not old
        assert "image_url" not in _types(projected[0])
        assert "media budget" in _texts(projected[0])
        assert projected[1] is tool
        assert projected[2] is latest
        # The checkpoint copy is never rewritten.
        assert _types(old) == ["text", "image_url"]

    def test_latest_user_media_is_protected_even_over_budget(self, budget):
        budget(50)
        old = _human("old", [100], user=False)
        latest = _human("latest", [1000])
        messages = [old, latest]

        projected = project_media_budget(messages)

        assert "image_url" not in _types(projected[0])
        assert projected[1] is latest
        assert _types(latest) == ["text", "image_url"]

    def test_remote_urls_are_not_counted(self, budget):
        budget(10)
        remote = HumanMessage(
            content=[
                {"type": "text", "text": "remote"},
                {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
            ]
        )
        latest = _human("latest", [1000])

        projected = project_media_budget([remote, latest])

        assert projected[0] is remote
        assert projected[1] is latest

    def test_everything_non_protected_is_dropped_when_the_budget_cannot_hold_any(self, budget):
        budget(10, preserve=False)
        old = _human("old", [100])
        tool = _tool([200])
        messages = [old, tool]

        projected = project_media_budget(messages)

        for message in projected:
            assert "image_url" not in _types(message)
            assert "media budget" in _texts(message)
        assert _types(old) == ["text", "image_url"]
        assert _types(tool) == ["text", "image_url"]

    def test_unstamped_transcript_protects_the_newest_media_human(self, budget):
        budget(50)
        old = _human("old", [1000], user=False)
        latest = _human("latest", [100], user=False)

        projected = project_media_budget([old, latest])

        assert "image_url" not in _types(projected[0])
        assert projected[1] is latest

    def test_audio_and_video_blocks_are_projected_too(self, budget):
        budget(10, preserve=False)
        message = HumanMessage(
            content=[
                {"type": "text", "text": "attachments"},
                _media("audio_url", 100),
                _media("video_url", 100),
            ]
        )

        projected = project_media_budget([message])

        assert _types(projected[0]) == ["text", "text", "text"]
        assert "[Attached audio]" in _texts(projected[0])
        assert "[Attached video]" in _texts(projected[0])


class TestMiddleware:
    def _request(self, messages: list) -> ModelRequest:
        return ModelRequest(model=_SentinelModel(), messages=messages)

    def test_request_override_leaves_the_original_request_untouched(self, budget):
        budget(50, preserve=False)
        request = self._request([AIMessage(content="done"), _human("old", [100])])

        projected = MediaBudgetProjectionMiddleware().wrap_model_call(request, lambda inner: inner)

        assert projected is not request
        assert "media budget" in _texts(projected.messages[1])
        assert _types(request.messages[1]) == ["text", "image_url"]

    def test_request_is_returned_unchanged_when_nothing_is_dropped(self, budget):
        budget(10_000)
        request = self._request([_human("hi", [10])])

        assert (
            MediaBudgetProjectionMiddleware().wrap_model_call(request, lambda inner: inner)
            is request
        )

    def test_skill_path_mode_drops_even_the_latest_user_media(self, budget, monkeypatch):
        budget(10_000)
        monkeypatch.setitem(MEDIA_PIPELINE, "main_llm_native_multimodal", "false")
        request = self._request([_human("hi", [10])])

        projected = MediaBudgetProjectionMiddleware().wrap_model_call(request, lambda inner: inner)

        assert "image_url" not in _types(projected.messages[0])
        assert "media budget" in _texts(projected.messages[0])

    def test_awrap_model_call_projects_before_the_handler(self, budget):
        budget(50, preserve=False)
        request = self._request([_human("hi", [100])])
        seen: list[Any] = []

        async def handler(inner):
            seen.append(inner)
            return AIMessage(content="ok")

        asyncio.run(MediaBudgetProjectionMiddleware().awrap_model_call(request, handler))

        assert len(seen) == 1
        assert "image_url" not in _types(seen[0].messages[0])

    def test_child_chain_carries_the_projection(self):
        from agent.tools.subagent.spawn.core import _build_child_middlewares

        chain = _build_child_middlewares(auxiliary_llm=None, main_llm_context_window=131_072)

        assert any(isinstance(mw, MediaBudgetProjectionMiddleware) for mw in chain)
