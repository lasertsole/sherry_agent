"""Image compression cascade with a built-in token gate (ZCode parity).

The gate (``base64_length / 8`` tokens) is a PER-IMAGE sending decision, not a
context-window estimator: Sherry's context accounting still uses the provider's
reported ``usage.input_tokens``. The cascade mirrors ZCode's Jimp flow with PIL:
small fit → format-preserving re-encode → resize to 2000px → 75/50/25% downsample
→ hard JPEG fallback (1000…200px). WebP is passed through when it fits and
compressed with PIL when it does not (ZCode cannot recompress WebP).
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, replace

from PIL import Image

__all__ = ["CompressedImage", "ImageTooLargeError", "prepare_for_model"]

_WEBP_MAGIC = (b"RIFF", b"WEBP")
_FORMAT_MIME = {
    "PNG": ("image/png", ".png"),
    "JPEG": ("image/jpeg", ".jpg"),
    "GIF": ("image/gif", ".gif"),
    "WEBP": ("image/webp", ".webp"),
    "BMP": ("image/bmp", ".bmp"),
}


class ImageTooLargeError(ValueError):
    """Every cascade candidate still exceeded the image budget."""


@dataclass(frozen=True)
class ImageBudget:
    max_base64_bytes: int
    max_raw_bytes: int
    max_tokens: int
    token_ratio: float


@dataclass(frozen=True)
class CompressedImage:
    """One prepared image: bytes, MIME/extension and the winning strategy."""

    data: bytes
    media_type: str
    extension: str
    width: int
    height: int
    strategy: str
    original_bytes: int

    @property
    def size(self) -> int:
        return len(self.data)


def _base64_encoded_length(raw_length: int) -> int:
    """ZCode's ``Elo``: base64 length for *raw_length* bytes."""
    return math.ceil(raw_length / 3) * 4


def _fits_image_budget(raw: bytes, budget: ImageBudget) -> bool:
    """ZCode's ``SQ`` gate: raw/base64 byte caps + ``base64 * 0.125`` tokens."""
    base64_len = _base64_encoded_length(len(raw))
    token_estimate = math.ceil(base64_len * budget.token_ratio)
    return (
        len(raw) <= budget.max_raw_bytes
        and base64_len <= budget.max_base64_bytes
        and token_estimate <= budget.max_tokens
    )


def _detect_format(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if raw.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "GIF"
    if raw.startswith(b"BM"):
        return "BMP"
    if len(raw) > 12 and raw[:4] == _WEBP_MAGIC[0] and raw[8:12] == _WEBP_MAGIC[1]:
        return "WEBP"
    return None


def _mime_and_ext(fmt: str | None) -> tuple[str, str]:
    return _FORMAT_MIME.get(fmt or "", ("image/jpeg", ".jpg"))


def _flatten_alpha(image: Image.Image) -> Image.Image:
    if image.mode not in ("RGBA", "LA", "P"):
        return image.convert("RGB")
    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    return Image.alpha_composite(background, rgba).convert("RGB")


def _encode(image: Image.Image, fmt: str, quality: int | None = None) -> bytes:
    out = io.BytesIO()
    if fmt == "PNG":
        image.save(out, format="PNG", optimize=True)
    elif fmt == "GIF":
        if getattr(image, "n_frames", 1) > 1:
            image.save(out, format="GIF", save_all=True, optimize=True)
        else:
            image.save(out, format="GIF", optimize=True)
    elif fmt == "BMP":
        image.save(out, format="BMP")
    elif fmt == "WEBP":
        image.save(out, format="WEBP", quality=quality or 80, method=6)
    else:
        _flatten_alpha(image).save(out, format="JPEG", quality=quality or 80, optimize=True)
    return out.getvalue()


def _resize(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    return image.resize(size, Image.Resampling.LANCZOS)


def _scaled(image: Image.Image, factor: float) -> Image.Image:
    width = max(1, int(image.width * factor))
    height = max(1, int(image.height * factor))
    return _resize(image, (width, height))


def _fit_to_max(image: Image.Image, max_dimension: int) -> Image.Image:
    longest = max(image.size)
    if longest <= max_dimension:
        return image
    scale = max_dimension / longest
    return _scaled(image, scale)


def _quality_ladder(fmt: str) -> tuple[int | None, ...]:
    if fmt in ("JPEG", "WEBP"):
        return (80, 60, 40, 20)
    return (None,)


def _try_candidates(
    image: Image.Image,
    fmt: str,
    budget: ImageBudget,
    strategy: str,
    max_dimension: int,
) -> CompressedImage | None:
    if max(image.size) > max_dimension:
        return None
    for quality in _quality_ladder(fmt):
        data = _encode(image, fmt, quality)
        if _fits_image_budget(data, budget):
            media_type, extension = _mime_and_ext(fmt)
            return CompressedImage(
                data=data,
                media_type=media_type,
                extension=extension,
                width=image.width,
                height=image.height,
                strategy=strategy,
                original_bytes=0,
            )
    return None


def _compress_webp(raw: bytes, budget: ImageBudget, max_dimension: int) -> CompressedImage:
    with Image.open(io.BytesIO(raw)) as opened:
        image = opened.copy()
    image = _fit_to_max(image, max_dimension)
    for factor in (1.0, 0.75, 0.5, 0.25):
        candidate = image if factor == 1.0 else _scaled(image, factor)
        found = _try_candidates(candidate, "WEBP", budget, "webp-compressed", max_dimension)
        if found is not None:
            return found
    # Hard fallback: JPEG q20 at decreasing sizes.
    for size in (1000, 800, 600, 400, 300, 200):
        candidate = _fit_to_max(image, size)
        found = _try_candidates(candidate, "JPEG", budget, "webp-jpeg-fallback", max_dimension)
        if found is not None:
            return found
    raise ImageTooLargeError("image cannot be compressed within the token budget")


def prepare_for_model(
    raw: bytes,
    *,
    max_dimension: int = 2000,
    max_tokens: int = 25000,
    token_ratio: float = 0.125,
    max_base64_bytes: int = 5 * 1024 * 1024,
) -> CompressedImage:
    """Return the first candidate that satisfies the per-image budget.

    Raises :class:`ImageTooLargeError` when every cascade step still misses the
    raw/base64/token caps, or :class:`PIL.UnidentifiedImageError` for non-image
    bytes (callers keep their existing fallback for that case).
    """
    budget = ImageBudget(
        max_base64_bytes=max_base64_bytes,
        max_raw_bytes=max_base64_bytes * 3 // 4,
        max_tokens=max_tokens,
        token_ratio=token_ratio,
    )
    fmt = _detect_format(raw)
    original_size = len(raw)

    with Image.open(io.BytesIO(raw)) as opened:
        width, height = opened.size
        image = opened.copy()

    if fmt == "WEBP":
        if _fits_image_budget(raw, budget):
            return CompressedImage(
                data=raw,
                media_type="image/webp",
                extension=".webp",
                width=width,
                height=height,
                strategy="webp-passthrough",
                original_bytes=original_size,
            )
        return _compress_webp(raw, budget, max_dimension)

    if _fits_image_budget(raw, budget) and max(width, height) <= max_dimension:
        media_type, extension = _mime_and_ext(fmt)
        return CompressedImage(
            data=raw,
            media_type=media_type,
            extension=extension,
            width=width,
            height=height,
            strategy="original",
            original_bytes=original_size,
        )

    # Format-preserving re-encode at the original size.
    for candidate_fmt in (fmt or "JPEG", "JPEG"):
        found = _try_candidates(image, candidate_fmt, budget, "reencoded", max_dimension)
        if found is not None:
            return replace(found, original_bytes=original_size)

    # Resize to the dimension cap, then downsample 75/50/25% of that size.
    base = _fit_to_max(image, max_dimension)
    for factor in (1.0, 0.75, 0.5, 0.25):
        candidate = base if factor == 1.0 else _scaled(base, factor)
        for candidate_fmt in ((fmt or "JPEG"), "JPEG"):
            found = _try_candidates(
                candidate,
                candidate_fmt,
                budget,
                "resized" if factor == 1.0 else "downsampled",
                max_dimension,
            )
            if found is not None:
                return replace(found, original_bytes=original_size)

    # Hard fallback: shrinking JPEG at q20.
    for size in (1000, 800, 600, 400, 300, 200):
        candidate = _fit_to_max(image, size)
        found = _try_candidates(candidate, "JPEG", budget, "jpeg-fallback", max_dimension)
        if found is not None:
            return replace(found, original_bytes=original_size)

    raise ImageTooLargeError(
        f"image cannot be compressed within the token budget "
        f"(original {original_size} bytes, max {budget.max_tokens} tokens)"
    )
