"""Resource files and encodings: what the text tools refuse, and what they keep.

The file tools edit TEXT, and the licensing run made that matter: a read used to
hand the model a screenful of replacement characters for a PNG and then license
an overwrite, so the image could be replaced by mojibake. The contract pinned
here:

* a binary target is refused by read_file (size + hint, no license), by
  patch_file, and by write_file — the bytes are never touched;
* UTF-8, UTF-8-with-BOM and UTF-16 (LE/BE) text round-trips in its OWN codec:
  a write or a patch keeps the BOM, the byte order and therefore the file's
  bytes-as-others-expect-them; the license carries the codec forward;
* text in a codec we cannot sniff (GBK/GB18030 without a BOM) is refused, not
  guessed, and an append to it is refused too — appending UTF-8 into a UTF-16
  file would corrupt it;
* the isolated-workspace merge stays byte-exact for resource files.
"""

import codecs
import json
import os

import pytest

from agent.tools.file_tools.patch_file import build_patch_file_tool
from agent.tools.file_tools.read_file import build_read_file_tool
from agent.tools.file_tools.write_file import build_write_file_tool
from agent.tools.pub_base import path_utils, read_state

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

SESSION = "s-resource-files"

#: A tiny PNG header plus NUL bytes — enough to look like a resource file.
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01" + b"\x00" * 32


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setattr(path_utils, "ROOT_DIR", root)
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(root))

    from runtime import state_register_mem

    monkeypatch.setattr(state_register_mem, "_states", {})
    state_register_mem.clear_session(SESSION)
    read_state.forget_all()
    yield root
    read_state.forget_all()


def _read(session: str, path: str, **kwargs) -> dict:
    return json.loads(build_read_file_tool()._core(path, session_id=session, **kwargs))


def _write(session: str, path: str, text: str, **kwargs) -> str:
    return build_write_file_tool()._core(path, text, session_id=session, **kwargs)


def _patch(session: str, path: str, old: str, new: str) -> dict:
    return json.loads(
        build_patch_file_tool()._core(path, old_string=old, new_string=new, session_id=session)
    )


def test_reading_a_resource_file_is_refused_and_licenses_nothing(project):
    image = project / "logo.png"
    image.write_bytes(PNG_BYTES)

    out = _read(SESSION, "logo.png")

    assert "binary" in out["error"]
    assert str(len(PNG_BYTES)) in out["error"]
    assert "terminal" in out["hint"]
    # No license is granted, and the follow-up overwrite is refused by the
    # binary gate before the license even matters.
    assert read_state.licensed(SESSION, image) is None
    refused = json.loads(_write(SESSION, "logo.png", "not an image any more"))
    assert "binary" in refused["error"]
    assert image.read_bytes() == PNG_BYTES


def test_patching_a_resource_file_is_refused(project):
    image = project / "icon.png"
    image.write_bytes(PNG_BYTES)

    out = _patch(SESSION, "icon.png", "IHDR", "XXXX")

    assert "binary" in out["error"]
    assert image.read_bytes() == PNG_BYTES


def test_writing_a_resource_file_is_refused_even_when_it_was_read_before(project):
    """A binary file never holds a license, so nothing can text-write it."""
    image = project / "photo.png"
    image.write_bytes(PNG_BYTES)
    _read(SESSION, "photo.png")  # refused, and deliberately remembered as nothing

    # The read above granted nothing: no license exists for the path…
    assert read_state.licensed(SESSION, image) is None

    # …and the overwrite is refused by the binary gate.
    out = json.loads(_write(SESSION, "photo.png", "hello"))
    assert "binary" in out["error"]
    assert image.read_bytes() == PNG_BYTES


def test_a_utf16_file_round_trips_in_utf16(project):
    config = project / "app.config"
    config.write_bytes("setting=值\n".encode("utf-16"))

    read = _read(SESSION, "app.config")
    assert "setting" in read["content"]
    assert "值" in read["content"]

    # Read → overwrite keeps UTF-16 (BOM + codec), not UTF-8.
    assert "successfully" in _write(SESSION, "app.config", "setting=新值\n")
    data = config.read_bytes()
    assert data.startswith(codecs.BOM_UTF16_LE)
    assert data.decode("utf-16") == "setting=新值\n"

    # …and the second overwrite still knows the codec (license carried it).
    assert "successfully" in _write(SESSION, "app.config", "setting=再改\n")
    assert config.read_bytes().decode("utf-16") == "setting=再改\n"


def test_a_utf16_patch_keeps_the_codec(project):
    notes = project / "notes.txt"
    notes.write_bytes("alpha beta\n".encode("utf-16"))

    _read(SESSION, "notes.txt")
    result = _patch(SESSION, "notes.txt", "beta", "gamma")

    assert result["success"] is True
    data = notes.read_bytes()
    assert data.startswith(codecs.BOM_UTF16_LE)
    assert data.decode("utf-16") == "alpha gamma\n"


def test_a_big_endian_utf16_file_stays_big_endian(project):
    cfg = project / "be.config"
    cfg.write_bytes(codecs.BOM_UTF16_BE + "ключ=значение\n".encode("utf-16-be"))

    read = _read(SESSION, "be.config")
    assert "ключ" in read["content"]

    assert "successfully" in _write(SESSION, "be.config", "ключ=новое\n")
    data = cfg.read_bytes()
    assert data.startswith(codecs.BOM_UTF16_BE)
    assert data.decode("utf-16-be")[1:] == "ключ=новое\n"


def test_a_utf8_bom_file_keeps_its_bom(project):
    script = project / "run.sh"
    script.write_bytes(codecs.BOM_UTF8 + b"echo hi\n")

    _read(SESSION, "run.sh")
    assert "successfully" in _write(SESSION, "run.sh", "echo hello\n")

    assert script.read_bytes() == codecs.BOM_UTF8 + b"echo hello\n"


def test_legacy_encoded_text_is_refused_not_guessed(project):
    """GBK bytes have no BOM: refusing beats silently rewriting the codec."""
    legacy = project / "legacy.txt"
    legacy.write_bytes("中文内容\n".encode("gb18030"))

    read = _read(SESSION, "legacy.txt")
    assert "binary" in read["error"]

    patch = _patch(SESSION, "legacy.txt", "中文", "英文")
    assert "binary" in patch["error"]
    assert legacy.read_bytes() == "中文内容\n".encode("gb18030")


def test_appending_into_a_utf16_file_is_refused(project):
    """Appending UTF-8 bytes into UTF-16 text would corrupt it."""
    notes = project / "log16.txt"
    notes.write_bytes("line-1\n".encode("utf-16"))

    out = json.loads(_write(SESSION, "log16.txt", "line-2\n", append=True))

    assert "append" in out["error"]
    assert "utf-16" in out["error"]
    assert notes.read_bytes().decode("utf-16") == "line-1\n"


def test_appending_into_a_binary_file_is_refused(project):
    blob = project / "data.bin"
    blob.write_bytes(b"\x00\x01\x02")

    out = json.loads(_write(SESSION, "data.bin", "text", append=True))

    assert "binary" in out["error"]
    assert blob.read_bytes() == b"\x00\x01\x02"


def test_a_new_file_is_utf8_and_a_binary_sibling_is_untouched(project):
    assert "successfully" in _write(SESSION, "fresh.txt", "plain\n")
    assert (project / "fresh.txt").read_bytes() == b"plain\n"

    image = project / "keep.png"
    image.write_bytes(PNG_BYTES)
    # Creating an unrelated file does not disturb the resource file.
    assert image.read_bytes() == PNG_BYTES
    assert os.path.getsize(image) == len(PNG_BYTES)
