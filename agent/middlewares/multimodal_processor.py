import io
import time
import base64
from PIL import Image
from typing import Any
from loguru import logger
from config import SRC_DIR
from pub_func import is_url

# Magic byte signatures → file extension
# Ordered by specificity (more bytes = earlier check)
_AUDIO_MAGIC: dict[bytes, str] = {
    b"\x52\x49\x46\x46": ".wav",   # RIFF (WAV)
    b"\x1a\x45\xdf\xa3": ".webm",  # WebM / Matroska (audio or video)
    b"\x4f\x67\x67\x53": ".ogg",   # Ogg (Vorbis/Opus)
    b"\x49\x44\x33": ".mp3",       # ID3 tag (MP3)
    b"\xff\xfb": ".mp3",           # MPEG audio frame sync 1 (MP3)
    b"\xff\xf3": ".mp3",           # MPEG audio frame sync 2 (MP3)
    b"\xff\xf2": ".mp3",           # MPEG audio frame sync 3
    b"\x66\x4c\x61\x43": ".flac",  # FLAC
}
_VIDEO_MAGIC: dict[bytes, str] = {
    b"\x1a\x45\xdf\xa3": ".webm",  # WebM (may also be pure audio — safe default)
    b"\x00\x00\x00\x18\x66\x74\x79\x70": ".mp4",  # ftyp box (MP4, MOV, etc.)
    b"\x00\x00\x00\x20\x66\x74\x79\x70": ".mp4",  # ftyp box variant
    b"\x00\x00\x00\x1c\x66\x74\x79\x70": ".mp4",  # ftyp box variant (small header)
}
_IMAGE_MAGIC: dict[bytes, str] = {
    b"\x89\x50\x4e\x47\x0d\x0a\x1a\x0a": ".png",   # PNG
    b"\xff\xd8\xff": ".jpg",                          # JPEG (SOI marker)
    b"\x47\x49\x46\x38": ".gif",                      # GIF89a / GIF87a
    b"\x42\x4d": ".bmp",                              # BMP
    b"\x49\x49\x2a\x00": ".tiff",                     # TIFF little-endian
    b"\x4d\x4d\x00\x2a": ".tiff",                     # TIFF big-endian
    b"\x52\x49\x46\x46": ".webp",                     # RIFF (WebP)
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


from langgraph.runtime import Runtime
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import BaseMessage, HumanMessage


class MultimodalProcessor(AgentMiddleware):
    def __init__(self, session_id: str):
        super().__init__()
        self._session_id: str = session_id

    @staticmethod
    def _strip_image_url_from_content(content: Any) -> str:
        """Extract text from a multimodal content list, stripping image_url items.

        Returns the concatenated text content. Handles str, dict, and list formats.
        """
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            return content.get("text", "")
        if isinstance(content, list):
            texts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", ""))
            return "\n".join(texts)
        return ""

    async def abefore_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        state_mes_list: list[BaseMessage] = state["messages"]
        last_mes: BaseMessage = state_mes_list[-1]

        if not isinstance(last_mes, HumanMessage):
            return None

        content: str | dict[str, Any] | list[dict[str, Any]] = getattr(last_mes, "content", None)

        if not isinstance(content, list):
            return None

        text_dict: dict[str, Any] | None = None
        image_path_list: list[str] = []
        audio_path_list: list[str] = []
        video_path_list: list[str] = []

        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    if text_dict is not None:
                        raise Exception("Only one text item allowed per input list")
                    text_dict = item

                elif item.get("type") == "image_url":
                    url: str = item.get("image_url", {}).get("url", "")

                    # Check if it's a URL (exclude data: scheme, which is a base64-embedded image)
                    if is_url(url) and not url.startswith('data:'):
                        image_path_list.append(url)
                        continue
                    else:
                        if url.startswith('data:image/'):
                            # Already has a prefix, use as-is
                            base64_data: str = url.split(",")[1]
                        else:
                            # No prefix, add one
                            base64_data: str = url

                        try:
                            image_bytes = base64.b64decode(base64_data)
                        except Exception as e:
                            logger.error(f"Base64 decode failed: {e}")
                            continue

                        try:
                            image = Image.open(io.BytesIO(image_bytes))
                        except Exception as e:
                            logger.error(f"Image decode failed: {e}")
                            continue

                        temp_dir = SRC_DIR / "mutil_temp"
                        temp_dir.mkdir(parents=True, exist_ok=True)
                        ext = _infer_extension(image_bytes, "image")
                        temp_path = temp_dir / f"{str(int(time.time() * 1000))}{ext}"
                        temp_path = temp_path.resolve()
                        image.save(temp_path)
                        logger.info("Image cached successfully!")
                        image_path_list.append(temp_path.as_posix())

                elif item.get("type") == "audio_url":
                    url: str = item.get("audio_url", {}).get("url", "")
                    # Check if it's a URL (exclude data: scheme)
                    if is_url(url):
                        audio_path_list.append(url)

                elif item.get("type") == "audio_bytes":
                    audio_bytes:bytes = item.get("audio_bytes")
                    if audio_bytes is None:
                        logger.error("Audio bytes is None!")
                        continue

                    temp_dir = SRC_DIR / "mutil_temp"
                    temp_dir.mkdir(parents=True, exist_ok=True)
                    ext = _infer_extension(audio_bytes, "audio")
                    temp_path = temp_dir / f"{str(int(time.time() * 1000))}{ext}"
                    temp_path = temp_path.resolve()
                    temp_path.write_bytes(audio_bytes)
                    logger.info(f"Audio cached successfully! (extension={ext})")
                    audio_path_list.append(temp_path.as_posix())

                elif item.get("type") == "video_url":
                    url: str = item.get("video_url", {}).get("url", "")
                    if is_url(url):
                        video_path_list.append(url)

                elif item.get("type") == "video_bytes":
                    video_bytes: bytes = item.get("video_bytes")
                    if video_bytes is None:
                        logger.error("Video bytes is None!")
                        continue

                    temp_dir = SRC_DIR / "mutil_temp"
                    temp_dir.mkdir(parents=True, exist_ok=True)
                    ext = _infer_extension(video_bytes, "video")
                    temp_path = temp_dir / f"{str(int(time.time() * 1000))}{ext}"
                    temp_path = temp_path.resolve()
                    temp_path.write_bytes(video_bytes)
                    logger.info(f"Video cached successfully! (extension={ext})")
                    video_path_list.append(temp_path.as_posix())

        if text_dict is None:
            text_dict = {"type": "text", "text": ""}

        if len(image_path_list) > 0:
            text_dict["text"] += f"[System: The user uploaded {len(image_path_list)} image(s). Location: {",".join(image_path_list)}. If you need to view the image(s), use the image_to_text skill.]"

        if len(audio_path_list) > 0:
            text_dict["text"] += f"[System: The user uploaded {len(audio_path_list)} audio(s). Location: {",".join(audio_path_list)}. If you need to view the audio(s), use the speech_to_text skill.]"

        if len(video_path_list) > 0:
            text_dict["text"] += f"[System: The user uploaded {len(video_path_list)} video(s). Location: {",".join(video_path_list)}. If you need to view the video(s), use the video_text_to_text skill.]"

        last_mes.content = [text_dict]

        # Strip image_url blocks from history messages (DeepSeek and similar models don't support image_url format)
        for mes in state_mes_list[:-1]:
            if not isinstance(mes, HumanMessage):
                continue
            mes_content = getattr(mes, "content", None)
            if isinstance(mes_content, list):
                text_only = self._strip_image_url_from_content(mes_content)
                mes.content = text_only if text_only else mes_content

    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
        # Clean up cached image files: delete if filename is not a pure numeric timestamp; delete if older than 7 days
        temp_dir = SRC_DIR / "mutil_temp"
        if not temp_dir.exists():
            return None

        now_ms: int = int(time.time() * 1000)
        seven_days_ms: int = 7 * 24 * 60 * 60 * 1000
        deadline_ms: int = now_ms - seven_days_ms

        deleted_count: int = 0
        for fpath in temp_dir.iterdir():
            if not fpath.is_file():
                continue
            stem: str = fpath.stem  # Filename without extension
            if not stem.isdigit():
                # Filename is tampered with or not in timestamp format, delete directly
                fpath.unlink()
                deleted_count += 1
                continue
            file_time_ms: int = int(stem)
            if file_time_ms < deadline_ms:
                fpath.unlink()
                deleted_count += 1

        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} expired cached images")

        return None