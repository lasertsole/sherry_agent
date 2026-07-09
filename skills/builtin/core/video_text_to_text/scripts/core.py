import sys
import cv2
import base64
from pathlib import Path
from loguru import logger
from pydantic import validate_call
from langchain_core.messages import HumanMessage

# Dynamically add project root to sys.path
current_file = Path(__file__).resolve()
project_root: Path = current_file.parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
from config import TEMP_DIR
from models import VTTT_model

MIN_DURATION_SEC = 5.0
MAX_DURATION_SEC = 60.0


def _validate_video_duration(video_path: str) -> float:
    """Check video duration is within [MIN_DURATION_SEC, MAX_DURATION_SEC].

    Returns:
        Duration in seconds.

    Raises:
        ValueError: duration out of bounds or cannot be read.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"Cannot open video file: {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    if fps <= 0 or total_frames <= 0:
        raise ValueError(f"Cannot determine video duration: {video_path}")
    duration = total_frames / fps
    if duration < MIN_DURATION_SEC:
        raise ValueError(
            f"Video too short ({duration:.1f}s). Minimum: {MIN_DURATION_SEC}s"
        )
    if duration > MAX_DURATION_SEC:
        raise ValueError(
            f"Video too long ({duration:.1f}s). Maximum: {MAX_DURATION_SEC}s"
        )
    return duration


def _image_to_data_url(path: str) -> str:
    """Read a local image file and return a data:image/...;base64, URL."""
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    ext = Path(path).suffix.lstrip(".").lower()
    mime = "jpeg" if ext in ("jpg", "jpeg") else "png"
    return f"data:image/{mime};base64,{b64}"

@validate_call
def vtt(video_path: str, query: str = "")-> str:
    # --- Duration check ---
    try:
        _validate_video_duration(video_path)
    except ValueError as e:
        err_mes:str = f"[Error] {e}"
        logger.error(err_mes)
        return err_mes

    # Primary path: send the raw video as base64 (works with some backends)
    try:
        with open(video_path, "rb") as f:
            video_b64 = base64.b64encode(f.read()).decode("utf-8")

        data_url = f"data:video/mp4;base64,{video_b64}"

        if query.strip() == "":
            query = "what happen in the video?"

        msg = HumanMessage(content=[
            {"type": "text", "text": query},
            {"type": "video_url", "video_url": {"url": data_url}},
        ])
        res = VTTT_model.invoke([msg])
        suc_mes: str = f"Video recognition completed, content:\n{res.content}"
        logger.debug(suc_mes)
        return suc_mes
    except Exception as e:
        warn_mes: str = (f"[warn] Primary video path failed: {e}, Video format may not be supported,"
                       f" try extracting video frames as input for the VLM model.")
        logger.warning(warn_mes)
        return  warn_mes

@validate_call
def vtt_fackback(video_path: str, query: str, interval_sec: float = 1.0)-> str:
    # --- Duration check ---
    try:
        _validate_video_duration(video_path)
    except ValueError as e:
        err_mes: str = f"[Error] {e}"
        logger.error(err_mes)
        return err_mes

    from pathlib import Path

    # Fallback: extract frames locally and send as base64 images
    output_dir = Path(TEMP_DIR / "multimedia")
    try:
        from skills.builtin.core.video_text_to_text.scripts import extract_frames

        frames = extract_frames(
            video_path,
            output_dir.as_posix(),
            threshold=0.3,
            interval_sec=interval_sec,
            prefix="frame",
        )
        logger.debug(f"Total frames extracted: {len(frames)}")

        if len(frames) == 0:
            err_mes: str = "No frames extracted from video."
            logger.error(err_mes)
            return err_mes

        image_dict: list[dict[str, str]] = [
            {"type": "image_url", "image_url": {"url": _image_to_data_url(r.image_path), "detail": "low"}}
            for r in frames
        ]

        if query.strip() == "":
            query = "what happen in the video?"
        msg = HumanMessage(content=[
            {"type": "text", "text": query},
            *image_dict,
        ])
        res = VTTT_model.invoke([msg])
        suc_mes: str = f"Video recognition completed, content:\n{res.content}"
        logger.debug(suc_mes)
        return suc_mes
    except Exception as e:
        err_mes: str = f"[Error] {e}"
        logger.error(err_mes)
        return err_mes
    finally:
        # Clean up extracted frame files
        if output_dir.exists():
            import shutil
            shutil.rmtree(output_dir, ignore_errors=True)
            logger.debug("Cleaned up extracted frames: %s", output_dir)