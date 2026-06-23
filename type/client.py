from enum import Enum
from typing import TypedDict
from pydantic import BaseModel

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