"""The media package's public surface.

The old ``pub/func/media/image.py`` download/base64 helpers had no live caller
and issued bare ``requests.get``/``requests.head`` calls with no SSRF guard, so
the module was removed. The compression API (``image_compress``) is what the
live send paths use. This pins both halves: the helpers cannot come back
through either package namespace, and the module file stays deleted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import pub.func
import pub.func.media as media

pytestmark = [pytest.mark.unit]

#: Functions the deleted module used to expose (or only export internally).
_REMOVED_NAMES = (
    "detect_image_format",
    "validate_and_fix_base64",
    "download_with_retry",
    "clean_and_validate_base64",
    "download_and_convert_to_base64",
    "check_if_image_and_convert_to_base64",
)


def test_only_the_compression_api_is_exported() -> None:
    assert media.__all__ == ["CompressedImage", "ImageTooLargeError", "prepare_for_model"]
    assert callable(media.prepare_for_model)


@pytest.mark.parametrize("name", _REMOVED_NAMES)
def test_the_removed_helpers_are_not_reachable(name: str) -> None:
    assert not hasattr(media, name)
    assert not hasattr(pub.func, name)


def test_the_dead_module_file_is_gone() -> None:
    assert media.__file__ is not None
    assert not (Path(media.__file__).parent / "image.py").exists()
