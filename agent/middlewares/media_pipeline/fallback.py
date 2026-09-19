"""Skill-path fallback for native multimodal requests.

``MultimodalProcessor`` decides per turn whether the main LLM receives the raw
media blocks (native) or a text-only rewrite that points at the builtin
``image_to_text`` / ``speech_to_text`` / ``video_text_to_text`` skills. When an
auto-mode native attempt is rejected by the model, ``LLMRetryMiddleware`` calls
:func:`apply_skill_fallback` to rewrite the request onto the skill path.
"""

from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

from config import SRC_DIR
from pub.func import is_url

from .media_handlers import MEDIA_TYPE_BY_ITEM, MediaPaths, _MEDIA_HANDLERS


def attach_media_hints(text_dict: dict[str, Any], paths: MediaPaths) -> None:
    """Append the media hints directly into the HumanMessage's text block.

    The hint must live on the HumanMessage (not a SystemMessage) so the
    persistence layer stores it and it stays visible for reasoning within the
    turn. It names the skill the model has to invoke, because the skill script
    result is the ground truth the reply must be based on.
    """
    if len(paths.image_hints) > 0:
        text_dict["text"] += (
            f"\n[Uploaded media] The user uploaded {len(paths.image_hints)} image(s). "
            f"Location: {','.join(paths.image_hints)}. "
            "You MUST use skill discovery to recognize this/these image(s): call the "
            "skill_view tool with name 'image_to_text' to read its SKILL.md. Then follow "
            "the skill's script instructions exactly (it tells you how to run the "
            "image_to_text recognition logic via the terminal tool). "
            "Do NOT answer based on guesswork — the recognition result reported by the "
            "skill script is the ground truth you must base your reply on."
        )
    if len(paths.audios) > 0:
        text_dict["text"] += (
            f"\n[Uploaded media] The user uploaded {len(paths.audios)} audio(s). "
            f"Location: {','.join(paths.audios)}. "
            "You MUST use skill discovery to transcribe this/these audio(s): call the "
            "skill_view tool with name 'speech_to_text' to read its SKILL.md. Then follow "
            "the skill's script instructions exactly (it tells you how to run the "
            "speech_to_text recognition logic via the terminal tool). "
            "Do NOT answer based on guesswork — the transcription result reported by the "
            "skill script is the ground truth you must base your reply on."
        )
    if len(paths.videos) > 0:
        text_dict["text"] += (
            f"\n[Uploaded media] The user uploaded {len(paths.videos)} video(s). "
            f"Location: {','.join(paths.videos)}. "
            "You MUST use skill discovery to process this/these video(s): call the "
            "skill_view tool with name 'video_text_to_text' to read its SKILL.md. Then follow "
            "the skill's script instructions exactly (it tells you how to run the "
            "video_text_to_text recognition logic via the terminal tool). "
            "Do NOT answer based on guesswork — the recognition result reported by the "
            "skill script is the ground truth you must base your reply on."
        )


def apply_skill_fallback(messages: list[BaseMessage], session_id: str) -> list[BaseMessage]:
    """Strip the media blocks from the last HumanMessage and attach skill hints.

    Called by LLMRetryMiddleware when a model call fails with
    ``multimodal_not_supported`` during an auto-mode native attempt. Returns a
    new messages list with the modified last message, leaving the input
    untouched; the message's ``additional_kwargs`` (media persistence) are
    carried over.
    """
    if not messages:
        return messages
    last_mes = messages[-1]
    if not isinstance(last_mes, HumanMessage):
        return messages
    content = getattr(last_mes, "content", None)
    if not isinstance(content, list):
        return messages

    text_dict: dict[str, Any] | None = None
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text_dict = dict(item)
            break
    if text_dict is None:
        text_dict = {"type": "text", "text": ""}

    attach_media_hints(text_dict, _rebuild_paths(last_mes, content, session_id))

    new_last = HumanMessage(content=[text_dict])
    new_last.additional_kwargs = dict(getattr(last_mes, "additional_kwargs", {}) or {})
    return [*messages[:-1], new_last]


def _rebuild_paths(mes: BaseMessage, content: list[Any], session_id: str) -> MediaPaths:
    """Rebuild the media paths for a media content list.

    Paths already recorded in ``additional_kwargs`` (written by the
    ``before_agent`` pass that processed this same content) are reused, so a
    ``data:``/base64 payload is never decoded a second time and no duplicate
    temp file is produced. Content whose media family has no recorded path is
    processed again; a remote image URL is always safe to re-process because it
    only contributes a hint and downloads nothing.
    """
    paths = _paths_from_additional_kwargs(mes) or MediaPaths()
    recorded = {
        "vision": bool(paths.image_hints or paths.persisted_images),
        "audio": bool(paths.audios),
        "video": bool(paths.videos),
    }

    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "")
        handler = _MEDIA_HANDLERS.get(item_type)
        media_type = MEDIA_TYPE_BY_ITEM.get(item_type)
        if handler is None or media_type is None:
            continue
        if recorded[media_type] and not _is_remote_image_url(item_type, item):
            continue
        handler.process(item, session_id, paths, SRC_DIR)

    return paths


def _paths_from_additional_kwargs(mes: BaseMessage) -> MediaPaths | None:
    """Read the persisted media paths, or None when nothing was recorded."""
    kwargs = getattr(mes, "additional_kwargs", None)
    if not isinstance(kwargs, dict):
        return None
    images = _as_path_list(kwargs.get("images"))
    audios = _as_path_list(kwargs.get("audios"))
    videos = _as_path_list(kwargs.get("videos"))
    if not (images or audios or videos):
        return None

    paths = MediaPaths()
    if images:
        paths.persisted_images = list(images)
        paths.image_hints = list(images)
    if audios:
        paths.audios = list(audios)
    if videos:
        paths.videos = list(videos)
    return paths


def _as_path_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _is_remote_image_url(item_type: str, item: dict[str, Any]) -> bool:
    if item_type != "image_url":
        return False
    url = item.get("image_url", {}).get("url", "")
    return isinstance(url, str) and is_url(url) and not url.startswith("data:")
