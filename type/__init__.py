from enum import Enum
from pydantic import BaseModel
from typing import List, Optional, TypedDict

# Multimodal message body
class MultiModalMessage(BaseModel):
    # Text content
    text: str

    # Images (multiple supported)
    image_path_list: Optional[List[str]] = None
    image_bytes_list: Optional[List[bytes]] = None
    image_base64_list: Optional[List[str]] = None

    # Audio (single only)
    audio_path_list: Optional[List[str]] = None
    audio_bytes_list: Optional[List[bytes]] = None

    # Video (single only)
    video_path_list: Optional[List[str]] = None
    video_bytes_list: Optional[List[bytes]] = None

class FileType(Enum):
    AUDIO = "audio"
    IMAGE = "image"
    VIDEO = "video"

class Chat(BaseModel):
    role: str
    content: str
    timestamp: str
    audio_path_list: Optional[List[str]] =  None
    image_path_list: Optional[List[str]] =  None
    video_path_list: Optional[List[str]] =  None

class File(TypedDict):
    content: bytes
    type: FileType
    extension: str # 后缀