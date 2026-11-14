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
from PIL import Image
from loguru import logger
from models import ITTT_model
from config.path import load_env
from pub.func.validator import is_url
from pub.func.validator.safe_fetch import safe_fetch
from langchain_core.messages import HumanMessage

# .env plus the .network overlay (network keys outrank the environment file)
load_env()

#: Cap on one remote image download (a hostile endpoint must not exhaust memory).
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024

#: safe_fetch refusal reasons → the model-visible explanation.
_DOWNLOAD_ERRORS = {
    "scheme": "only public http(s) URLs are supported",
    "ssrf": "the URL resolves to a private, loopback or link-local address",
    "oversize": f"the image exceeds the {_MAX_DOWNLOAD_BYTES // (1024 * 1024)} MiB download limit",
    "redirects": "too many redirects",
    "network": "the host could not be reached or the response was unreadable",
}


def _image_suffix(data: bytes) -> str:
    """File suffix from the image magic bytes (PIL also sniffs content)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"


@validate_call
def itt(
    image_path: str,
    user_text: str = "Please describe the image content in as much detail as possible.",
) -> str:
    """Recognize image content (supports local file path or URL)

    Args:
        image_path: Local image path or image URL
        user_text: Instruction for the image description, defaults to "Please describe the image content in as much detail as possible."

    Returns:
        The recognized text content from the image.
    """
    # ----- Phase 1: Get image data -----
    if is_url(image_path):
        # URL → download to a temp file through the shared SSRF guard.
        # safe_fetch refuses non-http(s) schemes, resolves the host once and
        # dials THAT address (DNS pinning), re-verifies every redirect hop and
        # caps the body — a user-supplied URL cannot reach loopback / RFC1918 /
        # the cloud metadata endpoint, and cannot exhaust memory.
        logger.debug(f"Downloading image from URL: {image_path}")
        fetched = safe_fetch(image_path, timeout=60, max_bytes=_MAX_DOWNLOAD_BYTES)
        if not fetched.ok or fetched.body is None:
            reason = _DOWNLOAD_ERRORS.get(fetched.reason, fetched.reason or "unknown error")
            err_mes = f"[Error] Image download failed: {reason}"
            logger.warning(err_mes)
            return err_mes
        if not 200 <= fetched.status < 300:
            err_mes = f"[Error] Image download failed: HTTP {fetched.status}"
            logger.warning(err_mes)
            return err_mes
        with tempfile.NamedTemporaryFile(suffix=_image_suffix(fetched.body), delete=False) as tmp:
            tmp.write(fetched.body)
            tmp_path = Path(tmp.name)
        logger.info(f"[info] Image downloaded to temp file: {tmp_path}")
        path = tmp_path
    else:
        path = Path(image_path)
        try:
            if not path.exists():
                info_mes: str = f"[warn] Image path does not exist: {repr(path)}"
                logger.debug(info_mes)
                return info_mes
        except Exception as e:
            err_mes: str = f"[error] Invalid file path: {image_path}, {repr(e)}"
            logger.exception(err_mes)
            return err_mes

    # ----- Phase 2: Verify image integrity -----
    try:
        with Image.open(path) as img:
            img.verify()
    except Exception as e:
        err_mes: str = f"[error] Not a valid image file: {image_path}, {repr(e)}"
        logger.exception(err_mes)
        if is_url(image_path):
            tmp_path.unlink(missing_ok=True)
        return err_mes

    # ----- Phase 3: Convert to base64 -----
    try:
        with Image.open(path) as img:
            img_format = img.format.lower()  # jpg, png, webp, etc.

        with open(path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
    except Exception as e:
        err_mes = f"[Error] Image conversion failed: {repr(e)}"
        logger.exception(err_mes)
        if is_url(image_path):
            tmp_path.unlink(missing_ok=True)
        return err_mes

    # ----- Phase 4: Call the vision model -----
    try:
        image_base64: str = f"data:image/{img_format};base64,{encoded_string}"
        content_list: list[dict[str, str]] = [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": image_base64}},
        ]

        res = ITTT_model.invoke([HumanMessage(content=content_list)])

        suc_mess: str = f"[success] Image recognition completed, content:\n{res.content}"
        logger.info(suc_mess)
        return suc_mess
    except Exception as e:
        err_mes: str = f"[Error] Vision model call failed: {e}"
        logger.exception(err_mes)
        return err_mes
    finally:
        # Clean up temp file if it was downloaded from a URL
        if is_url(image_path):
            tmp_path.unlink(missing_ok=True)
