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
        "from models.embed_model import core\n"
        "assert core._setup_done is False, 'import ran setup'\n"
        "assert core._backend is None, 'import detected backend'\n"
    )
    assert result.returncode == 0, result.stderr


def test_import_extract_model_does_not_export_mineru_env() -> None:
    code = (
        "import os\n"
        "assert 'MINERU_TOOLS_CONFIG_JSON' not in os.environ, 'import exported env'\n"
        "import models.extract_model.core  # noqa: F401\n"
        "assert 'MINERU_TOOLS_CONFIG_JSON' not in os.environ, 'import exported env'\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "MINERU_TOOLS_CONFIG_JSON"}
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_setup_embed_model_detects_then_downloads_once(monkeypatch, tmp_path) -> None:
    from models.embed_model import core

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
    from models.embed_model import core

    weights = tmp_path / "bge-m3-q8_0.gguf"
    weights.write_bytes(b"x")
    downloads: list[int] = []
    monkeypatch.setattr(core, "_setup_done", False)
    monkeypatch.setattr(core, "_detect_backend", lambda: ("local", True, None))
    monkeypatch.setattr(core, "_ensure_downloaded", lambda: downloads.append(1))
    monkeypatch.setattr(core, "_GGUF_MODEL_PATH", weights)

    core.setup_embed_model()

    assert downloads == [], "present weights must not re-download"


def test_setup_mineru_env_exports_config(monkeypatch) -> None:
    from models.extract_model import core

    monkeypatch.delenv("MINERU_TOOLS_CONFIG_JSON", raising=False)
    core.setup_mineru_env()

    assert os.environ["MINERU_TOOLS_CONFIG_JSON"] == core._MINERU_CONFIG_JSON


def test_build_mineru_model_triggers_env_setup(monkeypatch) -> None:
    from models.extract_model import core

    monkeypatch.delenv("MINERU_TOOLS_CONFIG_JSON", raising=False)

    assert core.build_mineru_model() is core.MinerUModel.get_instance()
    assert os.environ["MINERU_TOOLS_CONFIG_JSON"] == core._MINERU_CONFIG_JSON
