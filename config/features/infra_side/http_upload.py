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
    # Client-side staging budget for the SHA-256 cache; a larger upload skips
    # the cache lookup and streams through the chunk protocol.
    max_staged_bytes: int
    # Client cache TTL for a previously uploaded URL (the client mirrors it).
    read_cache_ttl_ms: int


HTTP_UPLOAD: HttpUploadConfig = {
    "max_image_bytes": 25 * 1024 * 1024,
    "max_audio_bytes": 100 * 1024 * 1024,
    "max_video_bytes": 500 * 1024 * 1024,
    "chunk_size_bytes": 512 * 1024,
    "max_staged_bytes": 64 * 1024 * 1024,
    "read_cache_ttl_ms": 30_000,
}
