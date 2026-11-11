"""HTTP upload size limits, in bytes."""

from typing import TypedDict


class HttpUploadConfig(TypedDict):
    """HTTP upload size limits, in bytes."""

    max_image_bytes: int
    max_audio_bytes: int
    max_video_bytes: int
    # Chunked upload protocol (ZCode parity): one POST per chunk, a complete
    # call assembles the bytes by content hash to deduplicate repeats.
    chunk_size_bytes: int
    # Client-side file lanes for a multi-attachment send (the browser opens
    # about six connections per host, so this stays conservative).
    max_concurrent_files: int
    # Client cache TTL for a previously uploaded URL (the client mirrors it).
    read_cache_ttl_ms: int


HTTP_UPLOAD: HttpUploadConfig = {
    "max_image_bytes": 25 * 1024 * 1024,
    "max_audio_bytes": 100 * 1024 * 1024,
    "max_video_bytes": 500 * 1024 * 1024,
    "chunk_size_bytes": 512 * 1024,
    "max_concurrent_files": 8,
    "read_cache_ttl_ms": 30_000,
}
