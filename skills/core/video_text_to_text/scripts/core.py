import sys
import base64
from typing import Any
from pathlib import Path

from loguru import logger
from langchain_core.messages import HumanMessage

# Dynamically add project root to sys.path
current_file = Path(__file__).resolve()
project_root: Path = current_file.parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from config import SRC_DIR
from models import VTTT_model
from skills.core.video_text_to_text.scripts import extract_frames

def vtt(video_path: str)-> None:
    # Primary path: use the video-capable model directly
    try:
        with open(video_path, "rb") as f:
            video_b64 = base64.b64encode(f.read()).decode("utf-8")

        data_url = f"data:video/mp4;base64,{video_b64}"

        content_list: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这个视频里发生了什么？"},
                    {"type": "video_url", "video_url": {"url": data_url}},
                ],
            }
        ]

        res = VTTT_model.invoke(HumanMessage(content=content_list))
        logger.info(f"Video recognition completed, content:\n{res.content}")
        return None
    except Exception as e:
        logger.error(f"[Error] Vision model call failed: {e}")

    # Fallback: extract frames locally and send as images
    try:
        frames = extract_frames(
            video_path,
            (SRC_DIR / "mutil_temp").as_posix(),
            threshold=0.3,
            interval_sec=1.0,
            prefix="frame",
        )
        logger.info(f"Total frames extracted: {len(frames)}")

        if len(frames) == 0:
            logger.error("No frames extracted from video.")
            return None

        image_paths: list[str] = [r.image_path for r in frames]

        content_list: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这个视频里发生了什么？"},
                    *[{"type": "video_url", "video_url": {"url": p}} for p in image_paths]
                ],
            }
        ]

        res = VTTT_model.invoke(HumanMessage(content=content_list))
        logger.info(f"Video recognition completed, content:\n{res.content}")
        return None
    except Exception as e:
        logger.error(f"[Error] Vision model call failed: {e}")