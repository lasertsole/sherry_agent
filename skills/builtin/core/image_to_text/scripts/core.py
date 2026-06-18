import sys
from pathlib import Path
from pydantic import validate_call

# Dynamically add project root to sys.path
current_file = Path(__file__).resolve()
project_root: Path = current_file.parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import base64
import tempfile
import requests
from PIL import Image
from loguru import logger
from models import ITT_model
from dotenv import load_dotenv
from pub_func.validator import is_url
from langchain_core.messages import HumanMessage

# Load environment variables
load_dotenv(project_root / ".env", override=True)

@validate_call
def itt(image_path: str, user_text: str = "Please describe the image content in as much detail as possible.")-> None:
    """Recognize image content (supports local file path or URL)

    Args:
        image_path: Local image path or image URL
        user_text: Instruction for the image description, defaults to "Please describe the image content in as much detail as possible."

    Returns:
        The recognized text content from the image.
    """
    # ----- Phase 1: Get image data -----
    if is_url(image_path):
        # URL → download to temp file
        logger.info(f"Downloading image from URL: {image_path}")
        try:
            resp = requests.get(image_path, stream=True, timeout=60)
            resp.raise_for_status()
            # Infer suffix from Content-Type
            content_type = resp.headers.get("Content-Type", "")
            suffix = ".png"
            if "jpeg" in content_type or "jpg" in content_type:
                suffix = ".jpg"
            elif "webp" in content_type:
                suffix = ".webp"
            elif "gif" in content_type:
                suffix = ".gif"

            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(resp.content)
                tmp_path = Path(tmp.name)

            logger.info(f"Image downloaded to temp file: {tmp_path}")
            path = tmp_path
        except Exception as e:
            logger.error(f"[Error] Failed to download image: {e}")
            return None
    else:
        path = Path(image_path)
        try:
            if not path.exists():
                logger.info(f"File does not exist: {image_path}")
                return None
        except Exception as e:
            logger.error(f"Invalid file path: {image_path}, {e}")
            return None

    # ----- Phase 2: Verify image integrity -----
    try:
        with Image.open(path) as img:
            img.verify()
    except Exception as e:
        logger.error(f"Not a valid image file: {image_path}, {e}")
        if is_url(image_path):
            tmp_path.unlink(missing_ok=True)
        return None

    # ----- Phase 3: Convert to base64 -----
    try:
        with Image.open(path) as img:
            img_format = img.format.lower()  # jpg, png, webp, etc.

        with open(path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
    except Exception as e:
        logger.error(f"[Error] Image conversion failed: {e}")
        if is_url(image_path):
            tmp_path.unlink(missing_ok=True)
        return None

    # ----- Phase 4: Call the vision model -----
    try:
        image_base64: str = f"data:image/{img_format};base64,{encoded_string}"
        content_list: list[dict[str, str]] = [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": image_base64}},
        ]

        res = ITT_model.invoke([HumanMessage(content=content_list)])

        logger.info(f"Image recognition completed, content:\n{res.content}")
    except Exception as e:
        logger.error(f"[Error] Vision model call failed: {e}")
    finally:
        # Clean up temp file if it was downloaded from a URL
        if is_url(image_path):
            tmp_path.unlink(missing_ok=True)