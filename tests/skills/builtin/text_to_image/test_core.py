"""The text_to_image skill script: artifact persistence + stdout URIs.

The skill runs through the terminal tool, so the two things that make it usable
are pinned here: the result is PRINTED (the terminal captures stdout; the old
script wrote nothing and the agent never learned where the image went) and the
bytes land in the session-scoped artifact store under the ``SHERRY_SESSION_ID``
the terminal tool injects.
"""

from __future__ import annotations

import pytest

from agent.tools.pub_base import artifact_store
from models.TTI_model import GeneratedImage, TTIError
from skills.builtin.text_to_image.scripts import core as script

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

_PNG = b"\x89PNG\r\n\x1a\n" + b"skill-png"


def _digest(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()[:16]


class _FakeTTI:
    def __init__(self, images=None, error: Exception | None = None):
        self.images = images if images is not None else [GeneratedImage(_PNG, ".png")]
        self.error = error
        self.calls: list[dict] = []

    def generate(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if self.error is not None:
            raise self.error
        return list(self.images)


@pytest.fixture
def artifacts(tmp_path):
    def _write(session_id, data, *, media_type="binary", ext=".bin", root=None):
        return artifact_store.write_artifact(
            session_id, data, media_type=media_type, ext=ext, root=tmp_path
        )

    return _write


@pytest.fixture(autouse=True)
def _session_env(monkeypatch):
    monkeypatch.setenv("SHERRY_SESSION_ID", "sess-t2i")


def test_success_prints_and_returns_artifact_uris(monkeypatch, artifacts, capsys):
    fake = _FakeTTI([GeneratedImage(_PNG, ".png"), GeneratedImage(_PNG + b"2", ".png")])
    monkeypatch.setattr(script, "TTI_model", fake)
    monkeypatch.setattr(script, "write_artifact", artifacts)

    result = script.generate_image("a red square", n=2, size="512x512", quality="hd")

    assert result.splitlines() == [
        "sherry-artifact://sess-t2i/image-" + _digest(_PNG) + ".png",
        "sherry-artifact://sess-t2i/image-" + _digest(_PNG + b"2") + ".png",
    ]
    stdout = capsys.readouterr().out
    assert stdout.split("\n")[:2] == result.splitlines()
    assert fake.calls[0] == {
        "prompt": "a red square",
        "size": "512x512",
        "quality": "hd",
        "n": 2,
        "model": None,
    }


def test_no_session_env_is_a_loud_refusal(monkeypatch, capsys):
    monkeypatch.delenv("SHERRY_SESSION_ID")
    fake = _FakeTTI()
    monkeypatch.setattr(script, "TTI_model", fake)

    result = script.generate_image("a cat")

    assert result == ""
    assert "SHERRY_SESSION_ID is not set" in capsys.readouterr().out
    assert fake.calls == [], "no provider call without an artifact namespace"


def test_provider_error_is_printed_not_swallowed(monkeypatch, capsys):
    monkeypatch.setattr(script, "TTI_model", _FakeTTI(error=TTIError("bad key")))

    result = script.generate_image("a cat")

    assert result == ""
    assert "[error] image generation failed: bad key" in capsys.readouterr().out


def test_unexpected_error_is_printed_not_raised(monkeypatch, capsys):
    monkeypatch.setattr(script, "TTI_model", _FakeTTI(error=RuntimeError("boom")))

    result = script.generate_image("a cat")

    assert result == ""
    assert "RuntimeError: boom" in capsys.readouterr().out


def test_an_unavailable_store_is_reported(monkeypatch, capsys):
    monkeypatch.setattr(script, "TTI_model", _FakeTTI())
    monkeypatch.setattr(script, "write_artifact", lambda *args, **kwargs: None)

    result = script.generate_image("a cat")

    assert result == ""
    assert "could not be persisted" in capsys.readouterr().out


def test_a_partial_store_failure_keeps_the_successful_uri(monkeypatch, capsys):
    fake = _FakeTTI([GeneratedImage(_PNG, ".png"), GeneratedImage(_PNG + b"2", ".png")])
    monkeypatch.setattr(script, "TTI_model", fake)

    def _first_only(session_id, data, *, media_type="binary", ext=".bin", root=None):
        if data == _PNG:
            return artifact_store.artifact_uri(session_id, "image-0000000000000000.png")
        return None

    monkeypatch.setattr(script, "write_artifact", _first_only)

    result = script.generate_image("a cat", n=2)

    assert result == "sherry-artifact://sess-t2i/image-0000000000000000.png"
    stdout = capsys.readouterr().out
    assert result in stdout
    assert "[warn] 1 generated image(s) could not be persisted" in stdout


def test_the_script_never_writes_the_global_static_dir():
    """The old script saved a timestamped file under STATIC_DIR; that path is gone."""
    assert not hasattr(script, "STATIC_DIR")
