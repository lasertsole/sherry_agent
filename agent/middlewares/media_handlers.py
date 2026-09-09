"""Per-media-type strategies for :class:`MultimodalProcessor`.

Each :class:`MediaItemHandler` processes one multimodal content-item type
(image_url / audio_url / audio_bytes / video_url / video_bytes): it decodes
or downloads the payload, writes the cached + persisted copies, and records
the resulting paths into the shared :class:`MediaPaths` accumulator.
"""

import abc
import io
import time
import base64
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image
from loguru import logger

from pub_func import is_url

# Magic byte signatures → file extension
# Ordered by specificity (more bytes = earlier check)
_AUDIO_MAGIC: dict[bytes, str] = {
    b"\x52\x49\x46\x46": ".wav",  # RIFF (WAV)
    b"\x1a\x45\xdf\xa3": ".webm",  # WebM / Matroska (audio or video)
    b"\x4f\x67\x67\x53": ".ogg",  # Ogg (Vorbis/Opus)
    b"\x49\x44\x33": ".mp3",  # ID3 tag (MP3)
    b"\xff\xfb": ".mp3",  # MPEG audio frame sync 1 (MP3)
    b"\xff\xf3": ".mp3",  # MPEG audio frame sync 2 (MP3)
    b"\xff\xf2": ".mp3",  # MPEG audio frame sync 3
    b"\x66\x4c\x61\x43": ".flac",  # FLAC
}
_VIDEO_MAGIC: dict[bytes, str] = {
    b"\x1a\x45\xdf\xa3": ".webm",  # WebM (may also be pure audio — safe default)
    b"\x00\x00\x00\x18\x66\x74\x79\x70": ".mp4",  # ftyp box (MP4, MOV, etc.)
    b"\x00\x00\x00\x20\x66\x74\x79\x70": ".mp4",  # ftyp box variant
    b"\x00\x00\x00\x1c\x66\x74\x79\x70": ".mp4",  # ftyp box variant (small header)
}
_IMAGE_MAGIC: dict[bytes, str] = {
    b"\x89\x50\x4e\x47\x0d\x0a\x1a\x0a": ".png",  # PNG
    b"\xff\xd8\xff": ".jpg",  # JPEG (SOI marker)
    b"\x47\x49\x46\x38": ".gif",  # GIF89a / GIF87a
    b"\x42\x4d": ".bmp",  # BMP
    b"\x49\x49\x2a\x00": ".tiff",  # TIFF little-endian
    b"\x4d\x4d\x00\x2a": ".tiff",  # TIFF big-endian
    b"\x52\x49\x46\x46": ".webp",  # RIFF (WebP)
}


def _infer_extension(data: bytes, kind: str) -> str:
    """Guess file extension from magic bytes.

    Args:
        data: Raw bytes of the file.
        kind: 'audio', 'video', or 'image' — which magic table to use.

    Returns:
        A file extension including the dot, e.g. '.mp3', '.mp4', '.png'.
        Falls back to '.mp3' for audio, '.mp4' for video, '.png' for image.
    """
    if kind == "image":
        magic_table = _IMAGE_MAGIC
        fallback = ".png"
    elif kind == "video":
        magic_table = _VIDEO_MAGIC
        fallback = ".mp4"
    else:
        magic_table = _AUDIO_MAGIC
        fallback = ".mp3"

    for signature, ext in magic_table.items():
        if data[: len(signature)] == signature:
            return ext
    return fallback


def _download_url_to_temp(url: str, session_id: str, kind: str, src_dir: Path) -> str | None:
    """Download a media URL into the session folder, persisting a durable copy.

    Writes two copies so the media stays reachable after mutil_temp is
    garbage-collected:
      - <src_dir>/<session_id>/mutil_temp/<ts><ext>   (short-lived, skills read)
      - <src_dir>/<session_id>/media/<ts><ext>        (durable, /media endpoint)

    Returns the persistent media/ path (posix-style) on success, or None on failure.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (EMA_AI_agent)"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        if not data:
            logger.error(f"Media download returned empty body: {url}")
            return None

        ext = _infer_extension(data, kind)
        timestamp = str(int(time.time() * 1000))

        temp_dir = src_dir / session_id / "mutil_temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_path = (temp_dir / f"{timestamp}{ext}").resolve()
        temp_path.write_bytes(data)

        media_dir = src_dir / session_id / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        media_path = (media_dir / f"{timestamp}{ext}").resolve()
        media_path.write_bytes(data)

        logger.debug(
            f"Media downloaded from URL: {url} -> {media_path.as_posix()} "
            f"(temp={temp_path.as_posix()}, extension={ext})"
        )
        return media_path.as_posix()
    except Exception as e:
        logger.error(f"Media download failed for {url}: {e}")
        return None


def _unwrap_media_bytes(item: dict[str, Any]) -> bytes | None:
    """Extract raw bytes from a media content item.

    Handles both shapes produced by _get_content_list / callers:
      - {"bytes": <bytes>}  (bytes passed directly as a dict wrapper)
      - raw <bytes>         (bytes passed directly)
    """
    media_bytes = item.get("audio_bytes") or item.get("video_bytes") or item
    if isinstance(media_bytes, dict):
        media_bytes = media_bytes.get("bytes")
    if not isinstance(media_bytes, (bytes, bytearray)):
        logger.error(f"Invalid media bytes payload: {type(media_bytes).__name__}")
        return None
    return bytes(media_bytes)


def _write_bytes_media(
    data: bytes, session_id: str, kind: str, src_dir: Path
) -> tuple[Path, Path, str]:
    """Write raw media bytes to mutil_temp + media; return (temp, media, ext)."""
    temp_dir = src_dir / session_id / "mutil_temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    ext = _infer_extension(data, kind)
    timestamp = str(int(time.time() * 1000))
    temp_path = (temp_dir / f"{timestamp}{ext}").resolve()
    temp_path.write_bytes(data)
    media_dir = src_dir / session_id / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    media_path = (media_dir / f"{timestamp}{ext}").resolve()
    media_path.write_bytes(data)
    return temp_path, media_path, ext


@dataclass
class MediaPaths:
    """Media file paths collected while processing one HumanMessage."""

    # Every image location (remote URLs + cached temp paths) for the hint text.
    image_hints: list[str] = field(default_factory=list)
    # Persisted media/ image copies for additional_kwargs["images"].
    persisted_images: list[str] = field(default_factory=list)
    # Persisted audio paths for the hint text and additional_kwargs["audios"].
    audios: list[str] = field(default_factory=list)
    # Persisted video paths for the hint text and additional_kwargs["videos"].
    videos: list[str] = field(default_factory=list)


class MediaItemHandler(abc.ABC):
    """Strategy for one multimodal content-item type."""

    @abc.abstractmethod
    def process(
        self, item: dict[str, Any], session_id: str, paths: MediaPaths, src_dir: Path
    ) -> None: ...


class ImageUrlHandler(MediaItemHandler):
    """image_url items: remote URLs pass through; data:/base64 payloads are
    decoded with PIL and saved to mutil_temp + media."""

    def process(
        self, item: dict[str, Any], session_id: str, paths: MediaPaths, src_dir: Path
    ) -> None:
        url: str = item.get("image_url", {}).get("url", "")

        # Check if it's a URL (exclude data: scheme, which is a base64-embedded image)
        if is_url(url) and not url.startswith("data:"):
            paths.image_hints.append(url)
            return

        if url.startswith("data:image/"):
            # Already has a prefix, use as-is
            base64_data: str = url.split(",")[1]
        else:
            # No prefix, add one
            base64_data: str = url

        try:
            image_bytes = base64.b64decode(base64_data)
        except Exception as e:
            logger.error(f"Base64 decode failed: {e}")
            return

        try:
            image = Image.open(io.BytesIO(image_bytes))
        except Exception as e:
            logger.error(f"Image decode failed: {e}")
            return

        temp_dir = src_dir / session_id / "mutil_temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        ext = _infer_extension(image_bytes, "image")
        temp_path = temp_dir / f"{str(int(time.time() * 1000))}{ext}"
        temp_path = temp_path.resolve()
        image.save(temp_path)
        logger.debug("Image cached successfully!")
        paths.image_hints.append(temp_path.as_posix())

        # Persist a copy into the session's media/ folder so the image
        # survives even after mutil_temp is garbage-collected. The
        # persistent path is stored in additional_kwargs["images"] and
        # later saved to the DB so history can render it after refresh.
        media_dir = src_dir / session_id / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        media_path = media_dir / f"{str(int(time.time() * 1000))}{ext}"
        media_path = media_path.resolve()
        image.save(media_path)
        logger.debug("Image persisted to media folder successfully!")
        paths.persisted_images.append(media_path.as_posix())


class AudioUrlHandler(MediaItemHandler):
    """audio_url items: downloaded to a local file so the speech_to_text
    skill receives a readable local path."""

    def process(
        self, item: dict[str, Any], session_id: str, paths: MediaPaths, src_dir: Path
    ) -> None:
        url: str = item.get("audio_url", {}).get("url", "")
        if is_url(url):
            local_path = _download_url_to_temp(url, session_id, "audio", src_dir)
            if local_path is not None:
                paths.audios.append(local_path)


class AudioBytesHandler(MediaItemHandler):
    """audio_bytes items: decoded and saved to mutil_temp + media."""

    def process(
        self, item: dict[str, Any], session_id: str, paths: MediaPaths, src_dir: Path
    ) -> None:
        media_bytes = _unwrap_media_bytes(item)
        if media_bytes is None:
            return

        temp_path, media_path, ext = _write_bytes_media(media_bytes, session_id, "audio", src_dir)
        logger.debug(
            f"Audio cached successfully! (temp={temp_path.as_posix()}, "
            f"persistent={media_path.as_posix()}, extension={ext})"
        )
        paths.audios.append(media_path.as_posix())


class VideoBytesHandler(MediaItemHandler):
    """video_bytes items: decoded and saved to mutil_temp + media."""

    def process(
        self, item: dict[str, Any], session_id: str, paths: MediaPaths, src_dir: Path
    ) -> None:
        media_bytes = _unwrap_media_bytes(item)
        if media_bytes is None:
            return

        temp_path, media_path, ext = _write_bytes_media(media_bytes, session_id, "video", src_dir)
        logger.debug(
            f"Video cached successfully! (temp={temp_path.as_posix()}, "
            f"persistent={media_path.as_posix()}, extension={ext})"
        )
        paths.videos.append(media_path.as_posix())


class VideoUrlHandler(MediaItemHandler):
    """video_url items: downloaded to a local file so the video_text_to_text
    skill receives a readable local path."""

    def process(
        self, item: dict[str, Any], session_id: str, paths: MediaPaths, src_dir: Path
    ) -> None:
        url: str = item.get("video_url", {}).get("url", "")
        if is_url(url):
            local_path = _download_url_to_temp(url, session_id, "video", src_dir)
            if local_path is not None:
                paths.videos.append(local_path)


_MEDIA_HANDLERS: dict[str, MediaItemHandler] = {
    "image_url": ImageUrlHandler(),
    "audio_url": AudioUrlHandler(),
    "audio_bytes": AudioBytesHandler(),
    "video_url": VideoUrlHandler(),
    "video_bytes": VideoBytesHandler(),
}
