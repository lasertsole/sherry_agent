from pydantic import BaseModel

class MultiModalMessage(BaseModel):
    # Text content
    text: str

    # Images (multiple supported)
    image_path_list: list[str] | None = None
    image_bytes_list: list[bytes] | None = None
    image_base64_list: list[str] | None = None

    # Audio (single only)
    audio_path_list: list[str] | None = None
    audio_bytes_list: list[bytes] | None = None

    # Video (single only)
    video_path_list: list[str] | None = None
    video_bytes_list: list[bytes] | None = None