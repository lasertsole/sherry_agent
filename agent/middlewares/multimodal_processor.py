import io
import time
import base64
from PIL import Image
from typing import Any
from loguru import logger
from config import SRC_DIR
from pub_func import is_url
from langgraph.runtime import Runtime
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import BaseMessage, HumanMessage, messages_to_dict

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

                        image_bytes = base64.b64decode(base64_data)
                        image = Image.open(io.BytesIO(image_bytes))

                        temp_dir = SRC_DIR / "mutil_temp"
                        temp_dir.mkdir(parents=True, exist_ok=True)
                        temp_path = temp_dir / f"{str(int(time.time() * 1000))}.png"
                        temp_path = temp_path.resolve()
                        image.save(temp_path, "PNG")
                        logger.info("Image cached successfully!")
                        image_path_list.append(temp_path.as_posix())

        # TODO 添加音频处理逻辑

        # TODO 添加视频处理逻辑

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