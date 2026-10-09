"""Native vision for TOOL-produced images (the browser screenshot path).

``core.py`` decides native-vs-skill for UPLOADED (human) media. A tool that
renders a PNG has no upload to keep: it answers with a path, so the model can
only *look* by running the ``image_to_text`` skill — a local vision model, a
weight download, a detour — even when the serving model reads images natively.

This module gives such a tool one call:

    native_image_content(session_id, text, image_path)

which returns ``text``, or ``[text, image_block]`` when the image may travel
natively. The policy is the tri-state the human path already uses
(``MEDIA_PIPELINE["main_llm_native_multimodal"]``), with ``"auto"`` asking the
capability cache about the SESSION's effective model — a session can override
the main model (``StateKey.LLM_MAIN_MODEL``), so the env key is only the
fallback. Unprobed (``"auto"``) attaches: the same first-try contract uploads
have, with :func:`scrub_tool_media` / ``apply_skill_fallback`` stripping the
block again for a model that turns out not to read images.

Fail-open everywhere: an unreadable file, an oversize payload, an unknown
session — the text alone is returned, which is the pre-existing behaviour.
"""

from __future__ import annotations

import base64
import mimetypes
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, ToolMessage

from config.features import MEDIA_PIPELINE

from .media_handlers import MEDIA_TYPE_BY_ITEM, MediaType

__all__ = ["effective_model_key", "native_image_content", "scrub_tool_media"]

_FAMILY_LABEL: dict[MediaType, str] = {"vision": "image", "audio": "audio", "video": "video"}
_FAMILY_SKILL: dict[MediaType, str] = {
    "vision": "image_to_text",
    "audio": "speech_to_text",
    "video": "video_text_to_text",
}


def effective_model_key(session_id: str | None) -> str:
    """``provider/model`` the session will actually call; ``""`` when unknown.

    A session-level model override (``PUT /sessions/model``) wins over the env
    main model — the override is what the thinking-control middleware swaps into
    the request, so it is also the model whose capabilities apply here.
    """
    if session_id:
        try:
            from runtime import state_register_mem
            from runtime.session.state_keys import StateKey

            override = state_register_mem.get_state(session_id, StateKey.LLM_MAIN_MODEL, None)
        except Exception:
            override = None
        if isinstance(override, dict) and override.get("model"):
            return f"{override.get('provider') or ''}/{override['model']}"
    try:
        from ..llm_capability_cache import get_model_key

        return get_model_key()
    except Exception:
        return ""


def _native_allowed(session_id: str | None) -> bool:
    """Whether this session's model may receive media blocks natively."""
    mode = MEDIA_PIPELINE.get("main_llm_native_multimodal", "auto")
    if mode == "true":
        return True
    if mode != "auto":
        return False  # "false" and any unknown value: the fail-safe skill path
    model_key = effective_model_key(session_id)
    provider, _, model = model_key.partition("/")
    if not model:
        return True  # unknown model: try native; the scrub/retry machinery recovers
    try:
        from ..llm_capability_cache import get_capability

        return get_capability(provider, model, "vision") != "unsupported"
    except Exception:
        return True


def _image_block(image_path: str | Path) -> dict[str, Any] | None:
    """A ``data:`` image block for *image_path*, or ``None`` when unusable."""
    try:
        path = Path(image_path)
        if (
            not path.is_file()
            or path.stat().st_size > MEDIA_PIPELINE["tool_media_attach_max_bytes"]
        ):
            return None
        payload = path.read_bytes()
    except OSError:
        return None
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(payload).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def native_image_content(
    session_id: str | None, text: str, image_path: str | Path
) -> str | list[dict[str, Any]]:
    """A tool result's content: *text*, or *text* plus the image block.

    The text ALWAYS stays (it names the saved path), so the skill path and the
    message row work identically whether or not the image was attached.
    """
    if not _native_allowed(session_id):
        return text
    block = _image_block(image_path)
    if block is None:
        return text
    return [{"type": "text", "text": text}, block]


def _tool_placeholder(family: MediaType, item_type: str, model_key: str) -> str:
    label = _FAMILY_LABEL[family]
    skill = _FAMILY_SKILL[family]
    model_label = model_key if model_key.strip("/") else "the current model"
    return (
        f"[Tool media] The attached {label} ({item_type}) was stripped from this request "
        f"because {model_label} does not support {label} content. The file path is named "
        f"in this tool result's text; use the '{skill}' skill to analyze it."
    )


def scrub_tool_media[T: BaseMessage](
    messages: Sequence[T],
    provider: str,
    model: str,
    model_key: str,
    *,
    force: bool = False,
) -> tuple[list[T], bool]:
    """Replace media blocks in TOOL results the model cannot read.

    Mirrors the human-message scrub for tool messages: a block whose family is
    cached ``"unsupported"`` becomes a text pointer. ``force`` strips every
    family regardless (``apply_skill_fallback`` calls it right after a
    multimodal rejection, before the cache has learned anything). The explicit
    ``"true"`` policy is left alone — the operator asked for native blocks.

    :returns: ``(messages, changed)`` — the original objects when nothing matched.
    """
    mode = MEDIA_PIPELINE.get("main_llm_native_multimodal", "auto")
    if mode == "false" and not force:
        return list(messages), False  # nothing is ever attached under this policy

    changed = False
    result: list[T] = []
    for message in messages:
        if not isinstance(message, ToolMessage) or not isinstance(message.content, list):
            result.append(message)
            continue
        new_content: list[Any] = []
        message_changed = False
        for item in message.content:
            family = (
                MEDIA_TYPE_BY_ITEM.get(item.get("type", "")) if isinstance(item, dict) else None
            )
            if family is None:
                new_content.append(item)
                continue
            if not force and mode == "auto":
                from ..llm_capability_cache import get_capability

                if get_capability(provider, model, family) != "unsupported":
                    new_content.append(item)
                    continue
            new_content.append(
                {
                    "type": "text",
                    "text": _tool_placeholder(family, str(item.get("type", "")), model_key),
                }
            )
            message_changed = True
        if not message_changed:
            result.append(message)
            continue
        changed = True
        result.append(message.model_copy(update={"content": new_content}))
    return result, changed
