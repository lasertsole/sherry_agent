import re
import time
import base64
import requests
from loguru import logger

from pub.func.media.image_compress import ImageTooLargeError, prepare_for_model


def detect_image_format(image_data: bytes) -> str | None:
    """Detect image format by file header signature."""
    IMAGE_SIGNATURES = {  # noqa: N806
        b"\xff\xd8\xff": "JPEG",
        b"\x89PNG\r\n\x1a\n": "PNG",
        b"GIF87a": "GIF",
        b"GIF89a": "GIF",
        b"RIFF": "WEBP",
        b"BM": "BMP",
    }

    for signature, fmt in IMAGE_SIGNATURES.items():
        if image_data.startswith(signature):
            if fmt == "WEBP" and len(image_data) > 12:
                if image_data[8:12] == b"WEBP":
                    return "WEBP"
                else:
                    return None
            return fmt

    return None


def validate_and_fix_base64(base64_string: str) -> str | None:
    """
    Validate and fix base64 string padding.

    Args:
        base64_string: Base64-encoded string.

    Returns:
        Fixed base64 string, or None if invalid.
    """
    try:
        # Strip optional data URI prefix
        if "," in base64_string:
            prefix, base64_data = base64_string.split(",", 1)
        else:
            prefix = ""
            base64_data = base64_string

        # Strip whitespace
        base64_data = base64_data.strip()

        missing_padding = len(base64_data) % 4
        if missing_padding:
            base64_data += "=" * (4 - missing_padding)
            logger.debug(f"Fixed base64 padding, added {4 - missing_padding} '='")

        # Verify the base64 is valid by attempting a decode
        base64.b64decode(base64_data)

        # Reassemble
        if prefix:
            return f"{prefix},{base64_data}"
        else:
            return base64_data

    except Exception as e:
        logger.exception(f"base64 validation failed: {e}")
        return None


def download_with_retry(url: str, timeout: int = 10, max_retries: int = 3) -> bytes | None:
    """
    Download a file with retry mechanism and proper request headers.

    Args:
        url: Download URL.
        timeout: Timeout in seconds.
        max_retries: Maximum number of retries.

    Returns:
        File binary data, or None on failure.
    """
    # Set request headers to mimic a browser
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://im.qq.com/",
        "Connection": "keep-alive",
    }

    for attempt in range(max_retries):
        try:
            logger.debug(f"Download attempt ({attempt + 1}/{max_retries}): {url[:100]}...")

            response = requests.get(url, headers=headers, timeout=timeout, stream=True)

            if response.status_code == 400:
                logger.error("HTTP 400 error - URL may require authentication or has expired")
                logger.error(f"Response headers: {dict(response.headers)}")
                return None
            elif response.status_code == 403:
                logger.error("HTTP 403 error - access denied, may need Cookie")
                return None
            elif response.status_code == 404:
                logger.error("HTTP 404 error - resource not found")
                return None

            response.raise_for_status()

            image_data = response.content

            if len(image_data) == 0:
                logger.warning(f"Downloaded content is empty (attempt {attempt + 1})")
                continue

            logger.debug(f"Download successful, size: {len(image_data)} bytes")
            return image_data

        except requests.exceptions.HTTPError as e:
            logger.exception(f"HTTP error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                wait_time = 2**attempt  # Exponential backoff
                logger.debug(f"Waiting {wait_time}s before retry...")
                time.sleep(wait_time)
            else:
                return None
        except Exception as e:
            logger.exception(f"Download failed (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2**attempt)
            else:
                return None

    return None


def clean_and_validate_base64(base64_string: str) -> str | None:
    """
    Clean and validate a base64 string, removing all illegal characters.

    Args:
        base64_string: Base64-encoded string (data URI or plain base64).

    Returns:
        Cleaned base64 string (data URI format), or None if invalid.
    """
    try:
        # Split prefix and data
        if "," in base64_string:
            prefix, base64_data = base64_string.split(",", 1)
            if not prefix.startswith("data:image/"):
                logger.warning(f"Invalid data URI prefix: {prefix[:50]}")
                return None
        else:
            prefix = ""
            base64_data = base64_string

        # Critical fix: remove all whitespace (spaces, newlines, tabs, etc.)
        base64_data = re.sub(r"\s+", "", base64_data)

        # Keep only valid base64 characters and padding
        base64_data = re.sub(r"[^A-Za-z0-9+/=]", "", base64_data)

        if not base64_data:
            logger.error("base64 data is empty")
            return None

        # base64 length must be a multiple of 4
        padding_needed = len(base64_data) % 4
        if padding_needed:
            base64_data += "=" * (4 - padding_needed)
            logger.debug(f"Fixed padding, added {4 - padding_needed} '='")

        # Verify base64 can be decoded correctly
        try:
            decoded = base64.b64decode(base64_data, validate=True)
            if len(decoded) == 0:
                logger.error("base64 decoded content is empty")
                return None
        except Exception as decode_err:
            logger.exception(f"base64 decode failed: {decode_err}")
            return None

        # Reassemble
        if prefix:
            result = f"{prefix},{base64_data}"
        else:
            result = base64_data

        logger.debug(
            f"base64 validation passed, length: {len(result)}, decoded size: {len(decoded)} bytes"
        )
        return result

    except Exception as e:
        logger.exception(f"base64 cleanup/validation failed: {e}")
        import traceback

        logger.debug(traceback.format_exc())
        return None


def download_and_convert_to_base64(
    url: str, timeout: int = 10, max_size_mb: int = 10
) -> str | None:
    """Download an image and convert it to base64."""
    try:
        logger.debug(f"Processing image URL: {url[:100]}...")

        # Use retry-enabled download function
        image_data = download_with_retry(url, timeout)

        if not image_data:
            logger.error("Download failed or content is empty")
            return None

        size_mb = len(image_data) / (1024 * 1024)
        if size_mb > max_size_mb:
            logger.warning(f"Image too large: {size_mb:.2f}MB > {max_size_mb}MB")
            return None

        logger.debug(f"Image downloaded, size: {len(image_data)} bytes ({size_mb:.2f}MB)")

        detected_format = detect_image_format(image_data)
        if not detected_format:
            logger.warning(f"Unable to identify image format, file header: {image_data[:20].hex()}")
            logger.warning("Will attempt to continue processing...")
            detected_format = "JPEG"

        logger.debug(f"Detected image format: {detected_format}")

        # Compress through the same cascade the live send path uses.
        try:
            prepared = prepare_for_model(image_data)
        except ImageTooLargeError as exc:
            logger.warning("Downloaded image kept uncompressed: {}", exc)
            prepared = None
        except Exception:  # noqa: BLE001 — not a decodable image; keep the bytes
            prepared = None
        compressed_data = prepared.data if prepared is not None else image_data
        mime_type = prepared.media_type if prepared is not None else "image/jpeg"

        compressed_size_mb = len(compressed_data) / (1024 * 1024)
        logger.debug(f"Size after compression: {compressed_size_mb:.2f}MB")

        # Convert to base64 (no line breaks)
        base64_bytes = base64.b64encode(compressed_data)
        base64_string = base64_bytes.decode("utf-8")

        if not base64_string or len(base64_string) == 0:
            logger.error("base64 conversion result is empty")
            return None

        data_uri = f"data:{mime_type};base64,{base64_string}"

        logger.debug(f"Built data URI, total length: {len(data_uri)} chars")

        # Clean and validate base64
        validated_uri = clean_and_validate_base64(data_uri)
        if not validated_uri:
            logger.error("base64 validation failed")
            return None

        logger.debug(
            f"✅ Image conversion successful, format: JPEG, base64 length: {len(validated_uri)} chars"
        )
        return validated_uri

    except Exception as e:
        logger.exception(f"Download or conversion failed: {e}")
        import traceback

        logger.debug(traceback.format_exc())
        return None


def check_if_image_and_convert_to_base64(
    url: str, timeout: int = 10, max_size_mb: int = 10
) -> tuple[bool, str | None, str | None, str | None]:
    """
    Check if a URL points to an image and, if so, convert it to base64.

    Args:
        url: Image URL.
        timeout: Request timeout in seconds.
        max_size_mb: Maximum allowed image size in MB.

    Returns:
        (is_image, content_type, file_format, base64_string)
    """
    try:
        # For QQ multimedia URLs, skip HEAD request and download directly
        if "multimedia.nt.qq.com.cn" in url or "qq.com" in url:
            logger.debug("Detected QQ multimedia URL, attempting direct download...")

            # Download and convert directly
            base64_str = download_and_convert_to_base64(url, timeout, max_size_mb)

            if base64_str:
                return True, "image/jpeg", "JPEG", base64_str
            else:
                return False, None, None, None

        # Non-QQ URL, use standard flow
        response = requests.head(url, timeout=timeout, allow_redirects=True)
        content_type = response.headers.get("Content-Type", "").lower()

        content_length = response.headers.get("Content-Length")
        if content_length:
            size_mb = int(content_length) / (1024 * 1024)
            if size_mb > max_size_mb:
                logger.warning(f"Image too large: {size_mb:.2f}MB > {max_size_mb}MB")
                return False, content_type, None, None

        if "image/" in content_type:
            file_format = content_type.split("/")[-1].upper()
            logger.debug(f"Content-Type indicates image: {content_type}, format: {file_format}")

            base64_str = download_and_convert_to_base64(url, timeout, max_size_mb)
            if base64_str:
                return True, content_type, file_format, base64_str
            else:
                return False, content_type, file_format, None

        logger.debug("Content-Type ambiguous, checking file header...")
        response = requests.get(url, timeout=timeout, stream=True)
        response.raise_for_status()

        header = response.raw.read(32)

        IMAGE_SIGNATURES = {  # noqa: N806
            b"\xff\xd8\xff": "JPEG",
            b"\x89PNG\r\n\x1a\n": "PNG",
            b"GIF87a": "GIF",
            b"GIF89a": "GIF",
            b"RIFF": "WEBP",
            b"BM": "BMP",
        }

        detected_format = None
        for signature, fmt in IMAGE_SIGNATURES.items():
            if header.startswith(signature):
                detected_format = fmt
                break

        if not detected_format:
            logger.debug(f"Not an image, Content-Type: {content_type}")
            response.close()
            return False, content_type, None, None

        response.close()
        base64_str = download_and_convert_to_base64(url, timeout, max_size_mb)

        if base64_str:
            content_type = f"image/{detected_format.lower()}"
            return True, content_type, detected_format, base64_str
        else:
            return False, content_type, detected_format, None

    except Exception as e:
        logger.exception(f"Check failed: {e}")
        import traceback

        logger.debug(traceback.format_exc())
        return False, None, None, None
