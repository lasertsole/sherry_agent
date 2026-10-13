"""Client-facing data models (API payloads shared with the frontend)."""

from enum import Enum
from typing import TypedDict
from pydantic import BaseModel


class FileType(Enum):
    """Kinds of media attachments a client payload can reference."""

    AUDIO = "audio"
    IMAGE = "image"
    VIDEO = "video"


class Chat(BaseModel):
    """A single chat message delivered to the client."""

    role: str
    content: str
    timestamp: str
    audio_path_list: list[str] | None = None
    image_path_list: list[str] | None = None
    video_path_list: list[str] | None = None


class File(TypedDict):
    """File attachment descriptor (path + media type)."""

    content: bytes
    type: FileType
    extension: str  # File extension
