"""Compression-time offload of inline media, mirroring DeepAgents.

When compression is about to summarize a message prefix out of the active
context, any inline media it carries (``data:`` URLs / base64 payloads) is
decoded and written to ``SESSIONS_DIR/<session_id>/media/`` by content hash
(deduplicated), and the block is replaced by a text pointer of the form
``[evicted to: <path>]``. The pointer reuses the eviction marker that
``Summarization._collect_evicted_refs`` already scans, so the path lands in
``SummaryDoc.evicted_refs`` and survives on the summary chain.

Only the range about to be summarized is rewritten; the preserved tail window
keeps its media blocks untouched (media is not stripped per turn). Every
failure is fail-open: a block that cannot be decoded or written becomes
``<media error="failed_to_offload" />`` instead of crashing the compression.
"""

from __future__ import annotations

import base64
import hashlib
import urllib.parse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.messages import AnyMessage
from loguru import logger

from agent.middlewares.media_pipeline.media_handlers import _infer_extension
from config import SESSIONS_DIR
from config.path import is_safe_session_segment
from pub.func.estimate_tokens import classify_media_family
from pub.func.message.eviction import EVICTION_PREFIX

__all__ = [
    "MEDIA_FAILED_PLACEHOLDER",
    "MEDIA_OFFLOAD_SUBDIR",
    "offload_inline_media",
]

# Subdirectory under SESSIONS_DIR/<session_id>/ (sibling of evicted/); removed
# wholesale by clear_session.
MEDIA_OFFLOAD_SUBDIR = "media"

# Model-visible placeholder for a media block that could not be offloaded.
MEDIA_FAILED_PLACEHOLDER = '<media error="failed_to_offload" />'

_INLINE_URL_KEYS = ("image_url", "audio_url", "video_url")
_BYTES_KEYS = ("audio_bytes", "video_bytes")


def offload_inline_media(messages: Sequence[AnyMessage], session_id: str) -> list[AnyMessage]:
    """Return *messages* with inline media in list content replaced by pointers.

    Unchanged messages are returned by reference; a rewritten message is a
    ``model_copy`` clone, so the caller's transcript keeps its original media
    blocks (the preserved window and graph state are never mutated).
    """
    rewritten: list[AnyMessage] = []
    for message in messages:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            rewritten.append(message)
            continue
        new_content, changed = _offload_content(content, session_id)
        if not changed:
            rewritten.append(message)
            continue
        rewritten.append(message.model_copy(update={"content": new_content}))
    return rewritten


def _offload_content(content: list[Any], session_id: str) -> tuple[list[Any], bool]:
    new_content: list[Any] = []
    changed = False
    for block in content:
        if not isinstance(block, dict):
            new_content.append(block)
            continue
        try:
            raw = _extract_inline_media(block)
        except Exception as exc:
            logger.warning("inline media decode failed, using placeholder: {}", exc)
            new_content.append(_failed_block())
            changed = True
            continue
        if raw is None:
            new_content.append(block)
            continue
        family = classify_media_family(block) or "unknown"
        path = _store_media(raw, family, session_id)
        if path is None:
            new_content.append(_failed_block())
        else:
            new_content.append({"type": "text", "text": f"{EVICTION_PREFIX}{path}]"})
        changed = True
    return new_content, changed


def _extract_inline_media(block: dict[str, Any]) -> bytes | None:
    """Return the inline bytes of a media block, or ``None`` when not inline.

    Remote ``http(s)`` URLs are not inline and stay untouched; only ``data:``
    payloads, bare ``base64`` fields and raw bytes fields are offloaded.
    """
    for key in _INLINE_URL_KEYS:
        value = block.get(key)
        if isinstance(value, dict):
            url = value.get("url")
            if isinstance(url, str) and url.startswith("data:"):
                return _decode_data_url(url)
    top_url = block.get("url")
    if isinstance(top_url, str) and top_url.startswith("data:"):
        return _decode_data_url(top_url)

    base64_value = block.get("base64")
    if isinstance(base64_value, str) and base64_value.strip():
        return _decode_base64(base64_value)

    for key in _BYTES_KEYS:
        value = block.get(key)
        data = value.get("bytes") if isinstance(value, dict) else value
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)

    source = block.get("source")
    if isinstance(source, dict):
        data = source.get("data")
        if isinstance(data, str) and data.strip():
            return _decode_base64(data)
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
    return None


def _decode_data_url(url: str) -> bytes:
    header, separator, payload = url.partition(",")
    if not separator:
        raise ValueError("malformed data URL")
    raw = _decode_base64(payload) if ";base64" in header else urllib.parse.unquote_to_bytes(payload)
    if not raw:
        raise ValueError("empty data URL payload")
    return raw


def _decode_base64(payload: str) -> bytes:
    raw = base64.b64decode(payload, validate=False)
    if not raw:
        raise ValueError("empty base64 payload")
    return raw


def _store_media(raw: bytes, family: str, session_id: str) -> str | None:
    """Write *raw* to the session media dir by content hash; ``None`` on failure."""
    if not is_safe_session_segment(session_id):
        return None
    directory = Path(SESSIONS_DIR) / session_id / MEDIA_OFFLOAD_SUBDIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("media offload directory unavailable: {}", exc)
        return None
    path = directory / f"{hashlib.sha256(raw).hexdigest()[:16]}{_extension(raw, family)}"
    if not path.exists():
        try:
            path.write_bytes(raw)
        except OSError as exc:
            logger.warning("media offload write failed: {}", exc)
            return None
    return str(path)


def _extension(raw: bytes, family: str) -> str:
    if family in ("image", "audio", "video"):
        return _infer_extension(raw, family)
    return ".bin"


def _failed_block() -> dict[str, str]:
    return {"type": "text", "text": MEDIA_FAILED_PLACEHOLDER}
