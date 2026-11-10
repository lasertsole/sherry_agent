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
    # Hard ceiling for one uploaded media payload, in bytes. A payload over
    # this limit is skipped before any disk write: it never enters MediaPaths
    # and never reaches the model; the model gets a text notice instead.
    # 20 MiB matches DeepAgents' CLI hard limit for inbound media.
    max_media_bytes: int
    # Video-specific ceiling (models accept larger video blocks than the
    # generic inbound payload cap). ZCode's hard limit is 30 MiB.
    video_max_bytes: int
    # Ceiling for attaching a TOOL-produced image (browser_screenshot) to its
    # result as a native block. The block is a data URL that rides the turn's
    # message row, so the cap sits far below the upload ceiling: over it the
    # tool keeps its text + path answer and the model uses the image_to_text
    # skill instead.
    tool_media_attach_max_bytes: int
    # Image compression cascade: the per-image token gate (base64 length / 8)
    # is a SENDING decision, not the context-window estimator. See
    # pub/func/media/image_compress.py.
    image_compress_enabled: bool
    image_compress_max_dimension: int
    image_compress_max_tokens: int
    image_compress_token_ratio: float
    # Per-request history media budget: images are kept newest-first within
    # this byte budget; the latest real user message's media is protected.
    history_media_budget_bytes: int
    preserve_latest_user_media: bool
    # Content-addressed artifact store for persisted uploads (SHA-256 names,
    # existence dedupe, ``sherry-artifact://`` references). Off = the legacy
    # timestamped media copy is used for every upload.
    artifact_store_enabled: bool


MEDIA_PIPELINE: MediaPipelineConfig = {
    "multimodal_temp_retention_days": 7,
    "main_llm_native_multimodal": "auto",
    "main_llm_silent_degradation_detection": True,
    "max_media_bytes": 20 * 1024 * 1024,
    "video_max_bytes": 30 * 1024 * 1024,
    "tool_media_attach_max_bytes": 2 * 1024 * 1024,
    "image_compress_enabled": True,
    "image_compress_max_dimension": 2000,
    "image_compress_max_tokens": 25000,
    "image_compress_token_ratio": 0.125,
    "history_media_budget_bytes": 75 * 1024 * 1024,
    "preserve_latest_user_media": True,
    "artifact_store_enabled": True,
}
