"""Explicit-initialization contract for embed_model / extract_model.

Importing either module must be side-effect-free; the side effects (backend
detection + eager GGUF download for embed; the MinerU config env export for
extract) now happen in an explicit, idempotent ``setup_*()`` called by the
factory / first use. These tests pin both halves: subprocess imports prove the
absence of import-time effects, the in-process tests prove the setup call is
equivalent and idempotent.
"""

import os
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        timeout=60,
    )


def test_import_embed_model_has_no_side_effects() -> None:
    result = _run(
        "from models.embed_model.text import core\n"
        "assert core._setup_done is False, 'import ran setup'\n"
        "assert core._backend is None, 'import detected backend'\n"
    )
    assert result.returncode == 0, result.stderr


def test_setup_embed_model_detects_then_downloads_once(monkeypatch, tmp_path) -> None:
    from models.embed_model.text import core

    downloads: list[int] = []
    monkeypatch.setattr(core, "_setup_done", False)
    monkeypatch.setattr(core, "_backend", None)
    monkeypatch.setattr(core, "_detect_backend", lambda: ("local", True, None))
    monkeypatch.setattr(core, "_ensure_downloaded", lambda: downloads.append(1))
    monkeypatch.setattr(core, "_GGUF_MODEL_PATH", tmp_path / "missing.gguf")

    core.setup_embed_model()

    assert core._backend == "local"
    assert core._use_local is True
    assert downloads == [1]

    core.setup_embed_model()
    assert downloads == [1], "setup must be idempotent"


def test_setup_embed_model_skips_download_when_weights_present(monkeypatch, tmp_path) -> None:
    from models.embed_model.text import core

    weights = tmp_path / "bge-m3-q8_0.gguf"
    weights.write_bytes(b"x")
    downloads: list[int] = []
    monkeypatch.setattr(core, "_setup_done", False)
    monkeypatch.setattr(core, "_detect_backend", lambda: ("local", True, None))
    monkeypatch.setattr(core, "_ensure_downloaded", lambda: downloads.append(1))
    monkeypatch.setattr(core, "_GGUF_MODEL_PATH", weights)

    core.setup_embed_model()

    assert downloads == [], "present weights must not re-download"
