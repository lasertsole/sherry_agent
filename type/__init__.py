from enum import Enum
from pydantic import BaseModel
from typing import TypedDict

# Multimodal message body
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

class FileType(Enum):
    AUDIO = "audio"
    IMAGE = "image"
    VIDEO = "video"

class Chat(BaseModel):
    role: str
    content: str
    timestamp: str
    audio_path_list: list[str] | None =  None
    image_path_list: list[str] | None =  None
    video_path_list: list[str] | None =  None

class File(TypedDict):
    content: bytes
    type: FileType
    extension: str # 后缀