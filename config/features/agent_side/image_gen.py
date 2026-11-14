"""Image generation (text-to-image) defaults for the ``image_generation`` tool.

Aligned with ZCode's image-generation surface: the model is configured through
the existing ``TTI_*`` family in ``.models``; these values bound one call (how
many images, the provider default size/quality and the HTTP timeout). The
per-model hard cap lives in ``models/TTI_model/core.py`` because it depends on
the requested model (``dall-e-3`` accepts one image, the gpt-image family up to
ten).
"""

from typing import TypedDict


class ImageGenConfig(TypedDict):
    """Bounds for one image-generation call."""

    #: Hard cap on ``n`` regardless of the model's own maximum.
    max_n: int
    #: Provider default size when the caller does not pass one.
    default_size: str
    #: Provider default quality when the caller does not pass one (empty = let
    #: the provider decide, which is the only safe default across the DALL·E /
    #: gpt-image quality vocabularies).
    default_quality: str
    #: HTTP timeout for the Images API call (and a secondary download).
    timeout_s: float


IMAGE_GEN: ImageGenConfig = {
    "max_n": 10,
    "default_size": "1024x1024",
    "default_quality": "",
    "timeout_s": 120.0,
}
