"""Media-pipeline settings (uploaded-media temp retention + native multimodal mode)."""

from typing import TypedDict


class MediaPipelineConfig(TypedDict):
    """Media-pipeline settings (uploaded-media temp retention + native multimodal mode)."""

    multimodal_temp_retention_days: int
    # Tri-state switch covering every media type (vision / audio / video):
    # "true" keeps the media blocks for the model, "false" always takes the
    # skill path, "auto" decides through the process-level capability cache
    # with skill fallback on a multimodal_not_supported model error.
    main_llm_native_multimodal: str


MEDIA_PIPELINE: MediaPipelineConfig = {
    "multimodal_temp_retention_days": 7,
    "main_llm_native_multimodal": "auto",
}
