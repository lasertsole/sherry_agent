"""Unit tests for type/message.py — MultiModalMessage model."""

import pytest
from type.message import MultiModalMessage


class TestMultiModalMessage:
    """Test MultiModalMessage Pydantic model."""

    def test_text_only(self):
        msg = MultiModalMessage(text="Hello world")
        assert msg.text == "Hello world"
        assert msg.image_path_list is None
        assert msg.image_bytes_list is None
        assert msg.image_base64_list is None
        assert msg.audio_path_list is None
        assert msg.audio_bytes_list is None
        assert msg.video_path_list is None
        assert msg.video_bytes_list is None

    def test_with_image_paths(self):
        msg = MultiModalMessage(
            text="Look at this",
            image_path_list=["/tmp/img1.png", "/tmp/img2.jpg"],
        )
        assert msg.image_path_list == ["/tmp/img1.png", "/tmp/img2.jpg"]

    def test_with_image_bytes(self):
        msg = MultiModalMessage(
            text="binary image",
            image_bytes_list=[b"\x89PNG\r\n", b"\xff\xd8\xff"],
        )
        assert len(msg.image_bytes_list) == 2

    def test_with_image_base64(self):
        msg = MultiModalMessage(
            text="base64 img",
            image_base64_list=["iVBORw0KGgo"],
        )
        assert msg.image_base64_list == ["iVBORw0KGgo"]

    def test_with_audio(self):
        msg = MultiModalMessage(
            text="audio msg",
            audio_path_list=["/tmp/audio.wav"],
            audio_bytes_list=[b"RIFF"],
        )
        assert msg.audio_path_list == ["/tmp/audio.wav"]
        assert msg.audio_bytes_list == [b"RIFF"]

    def test_with_video(self):
        msg = MultiModalMessage(
            text="video msg",
            video_path_list=["/tmp/vid.mp4"],
            video_bytes_list=[b"\x00\x00\x00"],
        )
        assert msg.video_path_list == ["/tmp/vid.mp4"]
        assert msg.video_bytes_list == [b"\x00\x00\x00"]

    def test_empty_text(self):
        msg = MultiModalMessage(text="")
        assert msg.text == ""

    def test_model_serialization(self):
        msg = MultiModalMessage(text="serialize me")
        d = msg.model_dump()
        assert d["text"] == "serialize me"
        assert d["image_path_list"] is None

    def test_model_validate_roundtrip(self):
        msg = MultiModalMessage(text="roundtrip", image_path_list=["a.png"])
        d = msg.model_dump()
        msg2 = MultiModalMessage.model_validate(d)
        assert msg2.text == "roundtrip"
        assert msg2.image_path_list == ["a.png"]
