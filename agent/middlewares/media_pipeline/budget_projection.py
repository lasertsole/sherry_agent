"""Byte-budgeted projection of history media blocks.

The projection used to be a private method on :class:`MultimodalProcessor`,
which only the MAIN profile carries. A subagent can read an image (``read_file``
is not main-only), so N turns of tool media would accumulate without any budget
on the child chain. This module is the extracted, profile-neutral half:

* :func:`project_media_budget` — the pure newest-first projection (no ``self``,
  no checkpoint mutation; it returns a new list only when something is dropped);
* :class:`MediaBudgetProjectionMiddleware` — the ``wrap_model_call`` layer that
  applies it in BOTH profiles (registered in ``agent/middlewares/specs.py`` as a
  required entry).

The checkpoint copy is never rewritten: the projection edits the request copy
the model sees, exactly like the capability scrub above it.
"""

from __future__ import annotations

from typing import Any, override

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import BaseMessage, HumanMessage
from loguru import logger

from config.features import MEDIA_PIPELINE
from pub.func.message.classify import is_user_message

__all__ = ["MediaBudgetProjectionMiddleware", "project_media_budget"]

_OMITTED_PLACEHOLDERS: dict[str, str] = {
    "image_url": (
        "[Attached image]\n"
        "[Media omitted from provider request to keep the request body under the "
        "configured media budget.]"
    ),
    "audio_url": (
        "[Attached audio]\n"
        "[Media omitted from provider request to keep the request body under the "
        "configured media budget.]"
    ),
    "video_url": (
        "[Attached video]\n"
        "[Media omitted from provider request to keep the request body under the "
        "configured media budget.]"
    ),
}
_MEDIA_TYPES = frozenset(_OMITTED_PLACEHOLDERS)


def _data_url_bytes(url: str) -> int:
    """Decoded payload size of a base64 data URL (0 for a remote URL).

    The base64 length formula avoids decoding megabytes of payload on every
    model call; a non-base64 data URL falls back to its UTF-8 length.
    """
    if not url.startswith("data:"):
        return 0
    header, separator, payload = url.partition(",")
    if not separator:
        return 0
    payload = payload.strip()
    if ";base64" not in header:
        return len(payload.encode("utf-8"))
    padding = len(payload) - len(payload.rstrip("="))
    return max(0, len(payload) * 3 // 4 - padding)


def _media_block_bytes(item: dict[str, Any]) -> tuple[str, int] | None:
    """``(block_type, byte_cost)`` for one media block, or None when it is not media.

    A remote URL is fetched by the provider and costs nothing here, so it is
    measured as 0 and never dropped.
    """
    block_type = item.get("type")
    if block_type not in _MEDIA_TYPES:
        return None
    raw = item.get(block_type)
    if isinstance(raw, dict):
        url = raw.get("url", "")
    else:
        url = raw if isinstance(raw, str) else ""
    return block_type, _data_url_bytes(url) if isinstance(url, str) else 0


def _latest_media_user_index(messages: list[BaseMessage]) -> int:
    """Index of the latest real user message, else the newest media Human.

    ``metadata.origin == "user"`` is the authoritative marker. A transcript
    without it (a compacted/resumed history, a unit test) falls back to the
    newest human message that carries media, which is the same message for
    every shape the server produces.
    """
    for index in range(len(messages) - 1, -1, -1):
        if is_user_message(messages[index]):
            return index
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, HumanMessage):
            continue
        content = getattr(message, "content", None)
        if isinstance(content, list) and any(
            isinstance(item, dict) and item.get("type") in _MEDIA_TYPES for item in content
        ):
            return index
    return -1


def project_media_budget(
    messages: list[BaseMessage],
    *,
    budget_bytes: int | None = None,
    preserve_latest: bool | None = None,
) -> list[BaseMessage]:
    """Keep history media newest-first within the byte budget.

    Every ``image_url`` / ``audio_url`` / ``video_url`` block is measured by its
    decoded data-URL size (a remote URL is fetched by the provider, so it costs
    nothing and is kept), the latest real user message's media is protected, and
    blocks that no longer fit are replaced by a text placeholder. A budget of
    ``0`` drops every non-protected block (the explicit skill-path mode); a
    negative budget disables the projection.

    The input list and its messages are never mutated. Returns ``messages``
    itself when nothing is dropped, so a caller can keep the original request
    object.
    """
    budget = int(
        budget_bytes
        if budget_bytes is not None
        else MEDIA_PIPELINE.get("history_media_budget_bytes", 0) or 0
    )
    if budget < 0:
        return messages
    preserve = bool(
        preserve_latest
        if preserve_latest is not None
        else MEDIA_PIPELINE.get("preserve_latest_user_media", True)
    )
    protected_index = _latest_media_user_index(messages) if preserve else -1

    blocks: list[tuple[int, int, str, int, bool]] = []
    for message_index, message in enumerate(messages):
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        for item_index, item in enumerate(content):
            if not isinstance(item, dict):
                continue
            measured = _media_block_bytes(item)
            if measured is None:
                continue
            block_type, size = measured
            blocks.append(
                (
                    message_index,
                    item_index,
                    block_type,
                    size,
                    message_index == protected_index,
                )
            )
    if not blocks:
        return messages

    protected_bytes = sum(size for *_, size, protected in blocks if protected)
    remaining = max(0, budget - protected_bytes)
    used = 0
    dropped: set[tuple[int, int]] = set()
    for message_index, item_index, _block_type, size, protected in sorted(
        (block for block in blocks if not block[4]),
        key=lambda block: block[0],
        reverse=True,
    ):
        if used + size <= remaining:
            used += size
            continue
        dropped.add((message_index, item_index))
    if not dropped:
        return messages

    projected: list[BaseMessage] = []
    for message_index, message in enumerate(messages):
        if not any(index == message_index for index, _ in dropped):
            projected.append(message)
            continue
        content = getattr(message, "content", None)
        assert isinstance(content, list)
        replacement: list[Any] = []
        for item_index, item in enumerate(content):
            if (message_index, item_index) not in dropped:
                replacement.append(item)
                continue
            block_type = str(item.get("type"))
            replacement.append(
                {
                    "type": "text",
                    "text": _OMITTED_PLACEHOLDERS.get(
                        block_type, _OMITTED_PLACEHOLDERS["image_url"]
                    ),
                }
            )
        projected.append(message.model_copy(update={"content": replacement}))
    return projected


class MediaBudgetProjectionMiddleware(AgentMiddleware):
    """Apply :func:`project_media_budget` to every model request, in BOTH profiles.

    The skill-path mode (``main_llm_native_multimodal=false``) must not let any
    media block ride a request, including one left from an earlier turn; that
    mode projects with a zero budget and no latest-message protection.
    """

    def _project(self, request: ModelRequest) -> ModelRequest:
        messages = list(request.messages)
        mode = MEDIA_PIPELINE.get("main_llm_native_multimodal", "auto")
        if mode in ("auto", "true"):
            projected = project_media_budget(messages)
        else:
            # "false"/unknown is the explicit skill path: no media block may
            # ride a request, including one left over from an earlier turn.
            projected = project_media_budget(messages, budget_bytes=0, preserve_latest=False)
        if projected is messages:
            return request
        return request.override(messages=projected)

    @override
    def wrap_model_call(self, request, handler):
        logger.bind(middleware=type(self).__name__).debug("wrap_model_call hook fired")
        return handler(self._project(request))

    @override
    async def awrap_model_call(self, request, handler):
        logger.bind(middleware=type(self).__name__).debug("awrap_model_call hook fired")
        return await handler(self._project(request))
