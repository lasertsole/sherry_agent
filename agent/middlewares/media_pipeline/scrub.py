"""Per-request capability scrub for native multimodal blocks.

Replaces, in a request copy only, every media block whose family the serving
model cannot process with a text placeholder that names the stripped media, its
recorded path and the matching builtin skill. State is never read for writing:
the caller rebuilds the request with ``ModelRequest.override(messages=...)``.
"""

from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

from pub.func import is_url

from ..llm_capability_cache import get_capability
from .fallback import _paths_from_additional_kwargs
from .media_handlers import MEDIA_TYPE_BY_ITEM, MediaPaths, MediaType

_FAMILY_LABEL: dict[MediaType, str] = {"vision": "image", "audio": "audio", "video": "video"}
_FAMILY_SKILL: dict[MediaType, str] = {
    "vision": "image_to_text",
    "audio": "speech_to_text",
    "video": "video_text_to_text",
}


def scrub_messages(
    messages: list[BaseMessage], provider: str, model: str, model_key: str
) -> tuple[list[BaseMessage], bool]:
    """Return ``(messages, changed)`` with unsupported media blocks replaced.

    Only ``HumanMessage`` multimodal content lists are inspected; every other
    message is carried over by reference. ``changed`` is False when no block
    needed replacement, letting the caller skip the request rebuild.
    """
    scrubbed: list[BaseMessage] = []
    changed = False
    for message in messages:
        if not isinstance(message, HumanMessage):
            scrubbed.append(message)
            continue
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            scrubbed.append(message)
            continue
        new_content, message_changed = _scrub_content(content, message, provider, model, model_key)
        if not message_changed:
            scrubbed.append(message)
            continue
        changed = True
        scrubbed.append(message.model_copy(update={"content": new_content}))
    return scrubbed, changed


def _scrub_content(
    content: list[Any],
    message: HumanMessage,
    provider: str,
    model: str,
    model_key: str,
) -> tuple[list[Any], bool]:
    recorded = _paths_from_additional_kwargs(message) or MediaPaths()
    counters: dict[MediaType, int] = {"vision": 0, "audio": 0, "video": 0}
    new_content: list[Any] = []
    changed = False
    for item in content:
        if not isinstance(item, dict):
            new_content.append(item)
            continue
        family = MEDIA_TYPE_BY_ITEM.get(item.get("type", ""))
        if family is None or get_capability(provider, model, family) != "unsupported":
            new_content.append(item)
            continue
        path = _media_path_for_block(item, family, recorded, counters)
        new_content.append({"type": "text", "text": _placeholder(item, family, path, model_key)})
        changed = True
    return new_content, changed


def _media_path_for_block(
    item: dict[str, Any],
    family: MediaType,
    recorded: MediaPaths,
    counters: dict[MediaType, int],
) -> str:
    """Resolve the on-disk location recorded for one media block.

    A remote ``image_url`` keeps its URL (the handler never copies it to disk);
    every other block maps to the persisted path at its position within the
    family. Returns a placeholder when no path was recorded.
    """
    if item.get("type") == "image_url":
        url = item.get("image_url", {}).get("url", "")
        if isinstance(url, str) and is_url(url) and not url.startswith("data:"):
            return url

    index = counters[family]
    counters[family] = index + 1
    recorded_paths: list[str] = {
        "vision": recorded.persisted_images,
        "audio": recorded.audios,
        "video": recorded.videos,
    }[family]
    if index < len(recorded_paths):
        return recorded_paths[index]
    return "(path unavailable)"


def _placeholder(item: dict[str, Any], family: MediaType, path: str, model_key: str) -> str:
    label = _FAMILY_LABEL[family]
    skill = _FAMILY_SKILL[family]
    model_label = model_key if model_key.strip("/") else "the current model"
    return (
        f"[Uploaded media] The attached {label} ({item.get('type', '')}) was stripped from "
        f"this request because {model_label} does not support {label} content. "
        f"Saved at: {path}. Use the '{skill}' skill to analyze it."
    )
