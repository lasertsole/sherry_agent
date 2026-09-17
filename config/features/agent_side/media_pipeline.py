"""Media-pipeline settings (uploaded-media temp-file retention)."""

from typing import TypedDict


class MediaPipelineConfig(TypedDict):
    """Media-pipeline settings (uploaded-media temp-file retention)."""

    multimodal_temp_retention_days: int


MEDIA_PIPELINE: MediaPipelineConfig = {
    "multimodal_temp_retention_days": 7,
}
