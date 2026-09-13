"""HTTP upload size limits, in bytes."""

from typing import TypedDict


class HttpUploadConfig(TypedDict):
    """HTTP upload size limits, in bytes."""

    max_image_bytes: int
    max_audio_bytes: int
    max_video_bytes: int


HTTP_UPLOAD: HttpUploadConfig = {
    "max_image_bytes": 25 * 1024 * 1024,
    "max_audio_bytes": 100 * 1024 * 1024,
    "max_video_bytes": 500 * 1024 * 1024,
}
