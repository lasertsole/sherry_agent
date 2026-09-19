"""Media-pipeline settings (uploaded-media temp retention + native multimodal mode)."""

from typing import TypedDict


class MediaPipelineConfig(TypedDict):
    """Media-pipeline settings (uploaded-media temp retention + native multimodal mode)."""

    multimodal_temp_retention_days: int
    # Tri-state switch covering every media type (vision / audio / video):
    # "true" keeps the media blocks for the model, "false" always takes the
    # skill path, "auto" decides through the process-level capability cache
    # with skill fallback on a multimodal_not_supported model error.
    # Any other value is treated as the fail-safe skill path.
    main_llm_native_multimodal: str
    # Auto-mode enhancement: when a native attempt succeeds with a reply that
    # self-reports media blindness, cache the media families present as
    # "unsupported" so later turns skip the doomed native probe.
    main_llm_silent_degradation_detection: bool


MEDIA_PIPELINE: MediaPipelineConfig = {
    "multimodal_temp_retention_days": 7,
    "main_llm_native_multimodal": "auto",
    "main_llm_silent_degradation_detection": True,
}
